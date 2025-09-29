from __future__ import annotations

import io
import json
import re
import tempfile
from typing import Dict, Tuple

import os
import requests
import streamlit as st
from bs4 import BeautifulSoup, NavigableString, Tag

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
# Ключ и настройки Ryne API из Streamlit Secrets / окружения
# ----------------------------
RYNE_API_KEY = st.secrets.get("RYNE_API_KEY") or os.getenv("RYNE_API_KEY", "")
# Можно переопределить URL эндпоинта в secrets: RYNE_API_URL = "https://ryne.ai/humanize"
RYNE_API_URL = st.secrets.get("RYNE_API_URL", os.getenv("RYNE_API_URL", "https://ryne.ai/humanize"))

# На случай, если поля в API отличаются, можно переопределить их через secrets
RYNE_INPUT_FIELD = st.secrets.get("RYNE_INPUT_FIELD", os.getenv("RYNE_INPUT_FIELD", "input"))
RYNE_FORMAT_FIELD = st.secrets.get("RYNE_FORMAT_FIELD", os.getenv("RYNE_FORMAT_FIELD", "format"))
RYNE_OUTPUT_FIELD = st.secrets.get("RYNE_OUTPUT_FIELD", os.getenv("RYNE_OUTPUT_FIELD", "output"))

# Дополнительные параметры к запросу (JSON), если нужны
_extra_params_raw = st.secrets.get("RYNE_EXTRA_PARAMS", os.getenv("RYNE_EXTRA_PARAMS", ""))
try:
    RYNE_EXTRA_PARAMS = json.loads(_extra_params_raw) if _extra_params_raw else {}
except Exception:
    RYNE_EXTRA_PARAMS = {}

# ----------------------------
# Оформление
# ----------------------------
st.set_page_config(page_title="Humanizer — Ryne API", page_icon="🛠️", layout="wide")
st.title("🛠️ Humanizer (Ryne API)")

# ----------------------------
# Хелперы
# ----------------------------
def is_html(text: str) -> bool:
    if not text:
        return False
    has_tag = bool(re.search(r"<([a-zA-Z][^>]*?)>", text))
    has_angle = "</" in text or "/>" in text
    return has_tag and has_angle


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
# Обёртка над Ryne /humanize
# ----------------------------
def ryne_humanize(text: str, output_format: str = "text") -> str:
    """
    Вызывает Ryne /humanize и возвращает результат.
    output_format: "text" | "html"

    Поля запроса/ответа настраиваются через секреты RYNE_*_FIELD.
    Доп. параметры можно передать через RYNE_EXTRA_PARAMS (JSON в secrets).
    """
    if not RYNE_API_KEY:
        raise RuntimeError(
            "Не найден RYNE_API_KEY. Укажите его в Streamlit secrets или переменной окружения."
        )

    headers = {
        "Authorization": f"Bearer {RYNE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload: Dict[str, object] = {
        RYNE_INPUT_FIELD: text,
        RYNE_FORMAT_FIELD: output_format,
    }
    # Подмешиваем любые доп. параметры
    if isinstance(RYNE_EXTRA_PARAMS, dict):
        payload.update(RYNE_EXTRA_PARAMS)

    resp = requests.post(RYNE_API_URL, headers=headers, json=payload, timeout=120)

    # Бросаем осмысленные ошибки
    try:
        resp.raise_for_status()
    except requests.HTTPError as http_err:
        try:
            err_json = resp.json()
        except Exception:
            err_json = {"detail": resp.text[:500]}
        raise RuntimeError(f"Ryne API ошибка {resp.status_code}: {err_json}") from http_err

    data = resp.json() if resp.content else {}

    # Пытаемся извлечь ответ гибко
    if isinstance(data, dict):
        # основной путь через настраиваемое имя поля
        out = data.get(RYNE_OUTPUT_FIELD)
        if out:
            return str(out)
        # часто встречающиеся альтернативы
        for key in ("result", "data", "text", "html"):
            if key in data:
                val = data[key]
                # если data -> внутри может лежать output
                if isinstance(val, dict):
                    return str(val.get("output") or val.get("text") or val.get("html") or "")
                return str(val)

    # если не разобрались — пробуем как есть
    return resp.text


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
        "Входной текст",
        height=280,
        placeholder="Вставьте сюда ваш текст / HTML. Результат вернёт Ryne /humanize.",
        label_visibility="collapsed",
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
    go = st.button("🚀 Запустить хуманизацию (Ryne)", type="primary", use_container_width=True)

# ----------------------------
# Основная логика
# ----------------------------
if go:
    if not input_text or not input_text.strip():
        st.error("Пожалуйста, вставьте текст или загрузите файл.")
    elif not RYNE_API_KEY:
        st.error(
            "Не найден RYNE_API_KEY. Укажите его в Streamlit secrets (Settings → Secrets) или в переменной окружения."
        )
    else:
        try:
            with st.spinner("Отправка в Ryne /humanize…"):
                # Вызываем один эндпоинт с нужным форматом
                if out_format == "HTML":
                    result = ryne_humanize(input_text, output_format="html")
                    out_kind = "html"
                else:
                    result = ryne_humanize(input_text, output_format="text")
                    out_kind = "txt"

            # Добавляем [Words: N] локально, если его нет
            try:
                if out_kind == "html":
                    visible_text = BeautifulSoup(result, "lxml").get_text(separator=" ").strip()
                    words_n = _word_count(visible_text)
                    if not re.search(r"\[Words:\s*\d+\]\s*$", result):
                        result = append_words_marker_to_html(result, words_n)
                else:
                    words_n = _word_count(result)
                    if not re.search(r"\[Words:\s*\d+\]\s*$", result):
                        result = f"{result}\n[Words: {words_n}]"
            except Exception:
                pass

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
                st.text_area("Предпросмотр текста", value=result, height=400, label_visibility="collapsed")

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
