from __future__ import annotations

"""
Streamlit Humanizer (stability & realism pass)
- Надёжная JSON-обработка (response_format="json_object")
- Батчинг текстовых узлов HTML (по символам)
- Контролируемая вариативность (температура, top_p, частотные штрафы)
- Опциональный маркер [Words: N]
- Исправлены мелкие баги (в т.ч. случайный символ 'ё')
- Доп. контекст для HTML-узлов (родительский тег)

⚠️ Эти инструменты предназначены для улучшения естественности текста, а не для нарушения академической честности.
"""

import io
import json
import math
import re
import tempfile
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Tuple

import streamlit as st
from bs4 import BeautifulSoup, NavigableString, Tag
from openai import OpenAI

# --- опциональные импорты для офисных файлов ---
try:
    from docx import Document  # .docx
except Exception:
    Document = None

try:
    import textract  # .doc (если установлен и есть системные зависимости)
except Exception:
    textract = None

import os

# ----------------------------
# Ключ и модель из Streamlit Secrets / окружения
# ----------------------------
API_KEY_DEFAULT = st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL_DEFAULT   = st.secrets.get("OPENAI_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5")

# ----------------------------
# Оформление
# ----------------------------
st.set_page_config(page_title="Humanizer — стабильная версия", page_icon="🛠️", layout="wide")
st.title("🛠️ Humanizer с сохранением структуры — стабильная версия")

st.caption(
    "Повышенная устойчивость: JSON-mode, батчинг длинных HTML, контроль вариативности, исправления багов."
)

# ----------------------------
# Хелперы
# ----------------------------

def is_html(text: str) -> bool:
    if not text:
        return False
    # Быстрый ранний тест по угловым скобкам
    if "</" in text or "/>" in text or re.search(r"<([a-zA-Z][^>]*?)>", text):
        return True
    # Фолбэк через парсер
    soup = BeautifulSoup(text, "lxml")
    return bool(soup.find())


def _word_count(s: str) -> int:
    tokens = re.findall(r"\w+", s, flags=re.UNICODE)
    return len(tokens)


def append_words_marker_to_html(html: str, n: int) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
        container = soup.body or soup
        p = soup.new_tag("p")
        p.string = f"[Words: {n}]"
        container.append(p)
        return str(soup)
    except Exception:
        return f"{html}\n[Words: {n}]"


# ------------ HTML разметка → маркировка текстовых узлов -------------
@dataclass
class NodeInfo:
    text: str
    parent_tag: str


def extract_text_nodes_as_mapping(html: str) -> Tuple[str, Dict[str, NodeInfo]]:
    """Оборачивает текстовые узлы в <span data-hid="..."> и собирает mapping id->NodeInfo."""
    soup = BeautifulSoup(html, "lxml")

    for bad in soup(["script", "style", "noscript"]):
        bad.extract()

    hid_counter = 0
    mapping: Dict[str, NodeInfo] = {}

    def tag_text_nodes(t: Tag) -> None:
        nonlocal hid_counter
        for child in list(t.children):
            if isinstance(child, NavigableString):
                text = str(child)
                if text and text.strip():
                    hid_counter += 1
                    hid = f"t{hid_counter}"
                    span = soup.new_tag("span")
                    span["data-hid"] = hid
                    span.string = text
                    child.replace_with(span)
                    parent_tag = t.name.lower() if isinstance(t, Tag) and t.name else "div"
                    mapping[hid] = NodeInfo(text=text, parent_tag=parent_tag)
            elif isinstance(child, Tag):
                tag_text_nodes(child)

    tag_text_nodes(soup.body or soup)
    return str(soup), mapping


def replace_text_nodes_from_mapping(html_with_ids: str, replacements: Dict[str, str]) -> str:
    soup = BeautifulSoup(html_with_ids, "lxml")
    for span in soup.find_all(attrs={"data-hid": True}):
        hid = span.get("data-hid")
        if hid in replacements:
            span.string = replacements[hid]
    # Удаляем служебные атрибуты
    for span in soup.find_all(attrs={"data-hid": True}):
        del span["data-hid"]
    return str(soup)


# ----------------------------
# Промпты (настраиваемые параметры)
# ----------------------------
PROMPT_PLAIN_TEXT_TPL = """You are an expert editor.
Edit the text so it reads naturally for a native speaker while preserving the original meaning, structure, and tone.

Language: use the SAME language as the input (auto-detect). Do NOT translate.

Requirements:
- Word count: keep within ±{wc_tol}% of the original. Append the final count as [Words: N].
- Structure: keep paragraph breaks, headings, list order/numbering, and overall section order.
- Formatting: keep punctuation, quotation marks, inline formatting (bold/italic/links/code), emojis, citation markers, and references.
- Facts & entities: do not add, remove, or alter information. Keep names, numbers, dates, URLs, and titles unchanged.
- Tone & register: preserve the author’s voice and level of formality.
- Style tweaks: use idiomatic phrasing, reduce repetitiveness, vary sentence length, simplify clunky constructions—without changing emphasis or intent.
- Non-text elements (code, formulas, tables): leave unchanged.
- Return ONLY the edited text—no explanations, no metadata (besides [Words: N]).
"""

PROMPT_PLAIN_TO_HTML_TPL = """You are an expert editor and HTML formatter.
Edit the text so it reads naturally (same language) and then output clean, semantic HTML.

Requirements:
- Word count: keep within ±{wc_tol}% of the original. Append the final count as a visible last paragraph: [Words: N].
- Preserve paragraph breaks, headings, list order/numbering, and section order (convert to HTML).
- Preserve punctuation, quotes, inline emphasis/links/code semantics; convert markers to <strong>/<em>/<a>/<code>.
- Do not alter facts, names, numbers, dates, URLs, or titles.
- Keep the author’s voice; improve fluency without changing intent.
- If the edited content contains at least one <table>, include at the VERY TOP a single <style> block:
  <style>
  table { border-collapse: collapse; }
  table, th, td { border: 1px solid #000; }
  </style>
- Non-text elements (code blocks, formulas, tables) should be kept as-is, wrapped in proper HTML tags if present.
- Return ONLY the HTML markup. No markdown, no comments, no code fences.
- Allowed tags: style (single block as above), p, h1..h4, ul/ol/li, blockquote, pre, code, a, strong, em, table/thead/tbody/tr/th/td, img (only if present), span.
"""

PROMPT_HTML_JSON_TPL = """You are an expert micro-editor.
You will receive a JSON object mapping {{id: text}} extracted from HTML text nodes.
For each value: edit to read naturally while preserving meaning, structure, tone, punctuation and references.
Use the SAME language as the value. Do NOT translate.

Constraints PER VALUE:
- Word count: keep within ±{wc_tol}% of that value’s original length.
- Absolutely DO NOT introduce or remove HTML tags; you are editing TEXT CONTENT ONLY.
- Return ONLY a valid JSON object with the SAME KEYS and improved string values. No extra text.

Reference (do not include in JSON): here are parent tag names for each id to help with style.
{{parents}}
"""

# ----------------------------
# Работа с моделью
# ----------------------------

def _openai_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key)


def _call_openai_json_map(
    client: OpenAI,
    model: str,
    mapping: Dict[str, NodeInfo],
    wc_tol: int,
    temperature: float,
    top_p: float,
    frequency_penalty: float,
    presence_penalty: float,
    seed: int | None = None,
) -> Dict[str, str]:
    """Вызывает модель в JSON-режиме батчами и возвращает объединённый dict id->text."""
    # Готовим простые структуры
    id_to_text = {k: v.text for k, v in mapping.items()}
    id_to_parent = {k: v.parent_tag for k, v in mapping.items()}

    # Батчинг по суммарной длине значений (символы), чтобы не упираться в лимит токенов
    batches: List[List[str]] = []
    current: List[str] = []
    acc_len = 0
    MAX_CHARS = 12000  # эмпирически безопасно для большинства моделей

    for k, v in id_to_text.items():
        add_len = len(k) + len(v) + 6
        if acc_len + add_len > MAX_CHARS and current:
            batches.append(current)
            current = []
            acc_len = 0
        current.append(k)
        acc_len += add_len
    if current:
        batches.append(current)

    out: Dict[str, str] = {}

    for i, batch_keys in enumerate(batches, 1):
        sub_map = {k: id_to_text[k] for k in batch_keys}
        parents_hint = {k: id_to_parent[k] for k in batch_keys}

        prompt = PROMPT_HTML_JSON_TPL.format(wc_tol=wc_tol, parents=json.dumps(parents_hint, ensure_ascii=False))

        # JSON-mode заставляет модель вернуть строго JSON
        kwargs = dict(
            model=model,
            messages=[
                {"role": "system", "content": "You are a careful, detail-oriented text editor."},
                {"role": "user", "content": prompt},
                {"role": "user", "content": json.dumps(sub_map, ensure_ascii=False)},
            ],
            temperature=temperature,
            top_p=top_p,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            response_format={"type": "json_object"},
        )
        if seed is not None:
            kwargs["seed"] = seed

        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or "{}"
        try:
            parsed = json.loads(content)
        except Exception:
            # Фолбэк на мягкий парсер
            m = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if not m:
                raise RuntimeError("Модель вернула не-JSON в батче %d" % i)
            parsed = json.loads(m.group(0))

        # sanity-check: все ключи на месте
        for k in batch_keys:
            if k not in parsed:
                parsed[k] = sub_map[k]
        out.update({k: str(parsed[k]) for k in batch_keys})

    return out


def _call_openai_text(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_text: str,
    temperature: float,
    top_p: float,
    frequency_penalty: float,
    presence_penalty: float,
    seed: int | None = None,
) -> str:
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        temperature=temperature,
        top_p=top_p,
        frequency_penalty=frequency_penalty,
        presence_penalty=presence_penalty,
    )
    if seed is not None:
        kwargs["seed"] = seed
    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


# ----------------------------
# Загрузка файлов
# ----------------------------

def read_text_file(uploaded) -> str:
    raw = uploaded.read().decode("utf-8", errors="ignore")
    return raw


def read_docx_file(uploaded) -> str:
    if Document is None:
        raise RuntimeError("Не установлен пакет python-docx. Установите: pip install python-docx")
    uploaded.seek(0)
    doc = Document(uploaded)
    blocks: List[str] = []
    for p in doc.paragraphs:
        if p.text is not None:
            blocks.append(p.text)
    for table in doc.tables:
        for row in table.rows:
            blocks.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(block for block in blocks if block is not None)


def read_doc_file(uploaded) -> str:
    if textract is None:
        raise RuntimeError("Для чтения .doc установите textract: pip install textract")
    uploaded.seek(0)
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=True) as tmp:
        tmp.write(uploaded.read())
        tmp.flush()
        data = textract.process(tmp.name)
    return data.decode("utf-8", errors="ignore")


# ----------------------------
# Генерация скачиваемых файлов
# ----------------------------

def build_docx_bytes(plain_text: str) -> bytes:
    if Document is None:
        raise RuntimeError("Для экспорта в .docx установите python-docx: pip install python-docx")
    doc = Document()
    for para in plain_text.split("\n"):
        doc.add_paragraph(para)
    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


# ----------------------------
# UI
# ----------------------------
col_in, col_opts = st.columns([2, 1], gap="large")

with col_in:
    st.markdown("#### Вставьте текст или HTML")
    input_text = st.text_area(
        "", height=280,
        placeholder="Вставьте сюда ваш текст / HTML. Структура и порядок элементов будут сохранены.",
    )
    uploaded = st.file_uploader(
        "…или загрузите файл (.html, .txt, .md, .docx, .doc)",
        type=["html", "txt", "md", "docx", "doc"],
        accept_multiple_files=False,
    )
    if uploaded is not None and not input_text:
        ext = (uploaded.name.split(".")[-1] or "").lower()
        try:
            if ext in {"html", "htm"}:
                input_text = read_text_file(uploaded)
            elif ext in {"txt", "md"}:
                input_text = read_text_file(uploaded)
            elif ext == "docx":
                input_text = read_docx_file(uploaded)
            elif ext == "doc":
                input_text = read_doc_file(uploaded)
            else:
                st.error("Неподдерживаемый формат файла.")
        except Exception as e:
            st.error(f"Не удалось прочитать файл: {e}")

with col_opts:
    st.markdown("#### Параметры модели")
    api_key = st.text_input("OPENAI_API_KEY", value=API_KEY_DEFAULT, type="password")
    model_id = st.text_input("Модель", value=MODEL_DEFAULT, help="Напр.: gpt-4o, gpt-4o-mini, gpt-5")

    temperature = st.slider("temperature", 0.0, 1.5, 0.7, 0.1)
    top_p        = st.slider("top_p",        0.1, 1.0, 1.0, 0.05)
    freq_pen     = st.slider("frequency_penalty", -2.0, 2.0, 0.2, 0.1)
    pres_pen     = st.slider("presence_penalty",  -2.0, 2.0, 0.0, 0.1)
    seed_opt     = st.text_input("seed (опционально)", value="", help="Для воспроизводимости. Оставьте пустым для случайности.")
    seed = int(seed_opt) if seed_opt.strip().isdigit() else None

    st.markdown("#### Выходной формат")
    out_format = st.radio("Формат выдачи", ["HTML", "Plain/Markdown"], index=0, horizontal=True)
    add_words_marker = st.checkbox("Добавлять [Words: N] в конец", value=False)
    wc_tol = st.slider("Допустимое изменение длины (±%)", 1, 20, 8)

    text_download_fmt = st.selectbox("Скачать текст как", ["TXT", "MD", "DOCX"], index=0, help="Применяется, когда результат — текст.")

    st.markdown("#### Обработать")
    go = st.button("🚀 Запустить обработку", type="primary", use_container_width=True)


# ----------------------------
# Основная логика
# ----------------------------
if go:
    if not (input_text and input_text.strip()):
        st.error("Пожалуйста, вставьте текст или загрузите файл.")
    elif not api_key:
        st.error(
            "Не найден OPENAI_API_KEY. Укажите его в Streamlit secrets (Settings → Secrets) или в переменной окружения."
        )
    else:
        try:
            client = _openai_client(api_key)
            with st.spinner("Обработка текста моделью…"):
                if is_html(input_text):
                    # HTML → JSON-замена с батчингом (сохраняем исходные теги 1:1)
                    html_with_ids, mapping = extract_text_nodes_as_mapping(input_text)
                    rewritten_map = _call_openai_json_map(
                        client=client,
                        model=model_id,
                        mapping=mapping,
                        wc_tol=wc_tol,
                        temperature=temperature,
                        top_p=top_p,
                        frequency_penalty=freq_pen,
                        presence_penalty=pres_pen,
                        seed=seed,
                    )
                    result_html = replace_text_nodes_from_mapping(html_with_ids, rewritten_map)

                    # Считаем слова по видимому тексту
                    if add_words_marker:
                        visible_text = BeautifulSoup(result_html, "lxml").get_text(separator=" ").strip()
                        words_n = _word_count(visible_text)
                        result_html = append_words_marker_to_html(result_html, words_n)

                    if out_format == "HTML":
                        result = result_html
                        out_kind = "html"
                    else:
                        plain = BeautifulSoup(result_html, "lxml").get_text(separator="\n")
                        plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
                        if add_words_marker:
                            # Для текста добавим маркер в конце отдельной строкой
                            words_n = _word_count(plain)
                            plain = f"{plain}\n[Words: {words_n}]"
                        result = plain
                        out_kind = "txt"
                else:
                    # Обычный текст/Markdown
                    if out_format == "HTML":
                        sys_prompt = PROMPT_PLAIN_TO_HTML_TPL.format(wc_tol=wc_tol)
                        result = _call_openai_text(
                            client, model_id, sys_prompt, input_text,
                            temperature, top_p, freq_pen, pres_pen, seed
                        )
                        out_kind = "html"
                    else:
                        sys_prompt = PROMPT_PLAIN_TEXT_TPL.format(wc_tol=wc_tol)
                        text_out = _call_openai_text(
                            client, model_id, sys_prompt, input_text,
                            temperature, top_p, freq_pen, pres_pen, seed
                        )
                        if not add_words_marker:
                            # Если маркер отключён — удалим его, если модель всё же добавила
                            text_out = re.sub(r"\s*\[Words:\s*\d+\]\s*$", "", text_out).rstrip()
                        result = text_out
                        out_kind = "txt"

            # Вывод и скачивание
            st.success("Готово!")
            if out_kind == "html":
                st.markdown("Просмотр HTML:")
                st.components.v1.html(result, height=600, scrolling=True)
                st.download_button(
                    label="⬇️ Скачать .html",
                    data=result.encode("utf-8"),
                    file_name="humanized.html",
                    mime="text/html",
                )
                with st.expander("Показать HTML-код"):
                    st.code(result, language="html")
            else:
                st.markdown("Предпросмотр текста:")
                st.text_area("", value=result, height=400)

                fmt = text_download_fmt.upper()
                if fmt == "TXT":
                    st.download_button(
                        label="⬇️ Скачать .txt",
                        data=result.encode("utf-8"),
                        file_name="humanized.txt",
                        mime="text/plain",
                    )
                elif fmt == "MD":
                    st.download_button(
                        label="⬇️ Скачать .md",
                        data=result.encode("utf-8"),
                        file_name="humanized.md",
                        mime="text/markdown",
                    )
                elif fmt == "DOCX":
                    try:
                        docx_bytes = build_docx_bytes(result)
                        st.download_button(
                            label="⬇️ Скачать .docx",
                            data=docx_bytes,
                            file_name="humanized.docx",
                            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )
                    except Exception as e:
                        st.error(f"Не удалось сформировать .docx: {e}")
        except Exception as e:
            # Читаемая ошибка без лишних символов/мусора
            st.error(f"Ошибка обработки: {e}")
