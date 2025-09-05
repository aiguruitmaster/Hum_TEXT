from __future__ import annotations

import io
import json
import re
import tempfile
from typing import Dict, Tuple

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

# ----------------------------
# Ключ и модель из Streamlit Secrets / окружения
# ----------------------------
import os

API_KEY  = st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL_ID = st.secrets.get("OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-5"))

# ----------------------------
# Оформление
# ----------------------------
st.set_page_config(page_title="Humanizer — сохранение структуры", page_icon="🛠️", layout="wide")
st.title("🛠️ Humanizer с сохранением структуры")

# ----------------------------
# Хелперы
# ----------------------------
def is_html(text: str) -> bool:
    if not text:
        return False
    has_tag = bool(re.search(r"<([a-zA-Z][^>]*?)>", text))
    has_angle = "</" in text or "/>" in text
    return has_tag and has_angle

def extract_text_nodes_as_mapping(html: str) -> Tuple[str, Dict[str, str]]:
    """Оборачивает текстовые узлы в <span data-hid="..."> и собирает mapping id->text."""
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript"]):
        bad.extract()

    hid_counter = 0
    mapping: Dict[str, str] = {}

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
                    mapping[hid] = text
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
    for span in soup.find_all(attrs={"data-hid": True}):
        del span["data-hid"]
    return str(soup)

def _safe_json_loads(maybe_json: str) -> Dict[str, str]:
    """Парсит JSON-объект из ответа модели. Пытается найти первый {...} блок."""
    try:
        return json.loads(maybe_json)
    except Exception:
        pass
    m = re.search(r"\{.*\}", maybe_json, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    raise ValueError("Не удалось распарсить JSON из ответа модели.")

def _word_count(s: str) -> int:
    tokens = re.findall(r"\w+", s, flags=re.UNICODE)
    return len(tokens)

def append_words_marker_to_html(html: str, n: int) -> str:
    """Добавляет в конец HTML видимый маркер [Words: N] как последний <p>."""
    try:
        soup = BeautifulSoup(html, "lxml")
        container = soup.body or soup
        p = soup.new_tag("p")
        p.string = f"[Words: {n}]"
        container.append(p)
        return str(soup)
    except Exception:
        return f"{html}\n[Words: {n}]"

# ----------------------------
# Промпты (обновлённые)
# ----------------------------

# 1) Обычный текст → возвращаем ОТРЕДАКТИРОВАННЫЙ ТЕКСТ с [Words: N] в конце
PROMPT_PLAIN_TEXT = """You are an expert human editor.

Goal
- Make the text read like it was written by a human native speaker.
- Keep meaning, facts, entities, URLs, numbers, dates, titles, and overall structure.

Language
- Use the SAME language as the input (auto-detect). Do NOT translate or normalize dialect/orthography.

Constraints
- Word count: keep within ±10% of the original. Append the final count as [Words: N].
- Preserve paragraph breaks, headings, list order/numbering, and section order.
- Preserve punctuation, quotation marks, inline formatting markers (bold/italic/links/code), emojis, citation markers, and references.

Style targets (human-like)
- Vary sentence length and rhythm; mix short and long sentences (“burstiness”).
- Prefer specific, idiomatic phrasing over generic templates; avoid stock openings like “In conclusion,” “As we can see,” etc.
- Use natural connectors (however, meanwhile, notably, still, that said, in fact, at times, for instance) but not in a repetitive pattern.
- Keep the author’s voice and register; do not add opinions or new facts.

Do NOT
- Do not add or remove factual content.
- Do not change any code blocks, formulas, or tables.

Output
- Return ONLY the edited text (no explanations, no code fences), with [Words: N] at the end.
"""

# 2) Обычный текст → красивый семантический HTML И тоже добавить [Words: N] как последний <p>.
PROMPT_PLAIN_TO_HTML = """You are an expert human editor and HTML formatter.

Goal
- Make the text read like a human native speaker wrote it.
- Then output clean, semantic HTML for the edited content.

Language
- Use the SAME language as the input (auto-detect). Do NOT translate or normalize dialect.

Constraints
- Word count: keep within ±10% of the original. Append the final count as the LAST paragraph: [Words: N].
- Preserve paragraph breaks, headings, list order/numbering, and section order; convert to equivalent HTML.
- Preserve punctuation, quotation marks, emphasis/links/code semantics; convert inline markers to <strong>/<em>/<a>/<code>.
- Keep facts, names, numbers, dates, URLs, and titles intact.

Style targets (human-like)
- Vary sentence length and rhythm; avoid template phrasing and repetitive transitions.
- Keep the author’s voice and register; improve fluency without changing intent.

Tables
- If there is at least one <table> in the edited content, include at the VERY TOP exactly one style block:
  <style>
  table { border-collapse: collapse; }
  table, th, td { border: 1px solid #000; }
  </style>
  Do not include any other CSS or inline styles.

Non-text
- Leave code blocks, formulas, tables as-is but wrap appropriately (<pre><code>, <table>, etc.) if present.

Allowed tags
- style (single block as above), p, h1..h4, ul/ol/li, blockquote, pre, code, a, strong, em,
  table/thead/tbody/tr/th/td, img (only if present in input), span.

Output
- Return ONLY the HTML markup. No markdown, no comments, no code fences, no explanations.
"""

# 3) HTML через JSON-замену (сохраняем исходную разметку 1:1); [Words: N] добавим в приложении.
PROMPT_HTML_JSON = """You are an expert micro-editor.

Input
- You will receive a JSON object mapping {id: text}, extracted from HTML text nodes.
- Edit each VALUE so it reads like natural human writing while keeping meaning and tone.

Language
- Use the SAME language as each value (auto-detect). Do NOT translate or normalize dialect.

Per-value constraints
- Word count: keep within ±10% of that value’s original length.
- Preserve punctuation, quotation marks, inline formatting markers present in the value,
  emojis, citation markers, and references.
- Absolutely DO NOT introduce or remove HTML tags (you edit TEXT ONLY).
- Keep facts, names, numbers, dates, URLs, and titles unchanged.

Style targets (human-like)
- Vary rhythm (short/long sentences where applicable); avoid generic templates and clichés.
- Maintain voice and register; do not add opinions or new information.

Output format (strict)
- Return ONLY a VALID JSON OBJECT with the SAME KEYS and improved string values.
- No surrounding text, no code fences, no comments.
- If any value is empty or whitespace, copy it unchanged.

Begin by returning the JSON object for the provided mapping.
"""

# ----------------------------
# Работа с моделями
# ----------------------------
def call_openai_json_map(api_key: str, mapping: Dict[str, str]) -> Dict[str, str]:
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "user", "content": PROMPT_HTML_JSON},
            {"role": "user", "content": json.dumps(mapping, ensure_ascii=False)},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    return _safe_json_loads(content)

def call_openai_rewrite_text(api_key: str, text: str) -> str:
    """Обычный текст → отредактированный текст (+[Words: N])."""
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "user", "content": PROMPT_PLAIN_TEXT},
            {"role": "user", "content": text},
        ],
    )
    return (resp.choices[0].message.content or "").strip()

def call_openai_rewrite_text_to_html(api_key: str, text: str) -> str:
    """Обычный текст → красивый семантический HTML с финальным <p>[Words: N]</p>."""
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "user", "content": PROMPT_PLAIN_TO_HTML},
            {"role": "user", "content": text},
        ],
    )
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
    blocks = []
    for p in doc.paragraphs:
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
# UI: ввод и выходной формат
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
        accept_multiple_files=False
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
    st.markdown("#### Выходной формат")
    out_format = st.radio("Формат выдачи", ["HTML", "Plain/Markdown"], index=0, horizontal=True)
    text_download_fmt = st.selectbox("Скачать текст как", ["TXT", "MD", "DOCX"], index=0, help="Применяется, когда результат — текст.")
    st.markdown("#### Обработать")
    go = st.button("🚀 Запустить хуманизацию", type="primary", use_container_width=True)

# ----------------------------
# Основная логика
# ----------------------------
if go:
    if not input_text or not input_text.strip():
        st.error("Пожалуйста, вставьте текст или загрузите файл.")
    elif not API_KEY:
        st.error(
            "Не найден OPENAI_API_KEY. Укажите его в Streamlit secrets "
            "(Settings → Secrets) или в переменной окружения."
        )
    else:
        try:
            with st.spinner("Обработка текста моделью…"):
                if is_html(input_text):
                    # HTML → JSON-замена (сохраняем исходные теги 1:1). [Words: N] добавляем после сборки.
                    html_with_ids, mapping = extract_text_nodes_as_mapping(input_text)
                    rewritten_map = call_openai_json_map(API_KEY, mapping)
                    result_html = replace_text_nodes_from_mapping(html_with_ids, rewritten_map)

                    # Считаем слова по видимому тексту и добавляем маркер
                    visible_text = BeautifulSoup(result_html, "lxml").get_text(separator=" ").strip()
                    words_n = _word_count(visible_text)
                    result_html = append_words_marker_to_html(result_html, words_n)

                    if out_format == "HTML":
                        result = result_html
                        out_kind = "html"
                    else:
                        plain = BeautifulSoup(result_html, "lxml").get_text(separator="\n")
                        plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
                        result = plain
                        out_kind = "txt"
                else:
                    # Обычный текст/Markdown
                    if out_format == "HTML":
                        # Красивый HTML — просим модель также добавить [Words: N] как последний параграф.
                        result = call_openai_rewrite_text_to_html(API_KEY, input_text)
                        out_kind = "html"
                    else:
                        # Отредактированный текст с [Words: N] в конце (добавляет модель)
                        result = call_openai_rewrite_text(API_KEY, input_text)
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
            st.error(f"Ошибка обработки: {e}")
