import os
import io
import json
import re
from typing import Tuple, Optional

import streamlit as st
import requests
from bs4 import BeautifulSoup
from docx import Document


# =============== Page config ===============
st.set_page_config(page_title="Ryne — AI Score + Читабельность/HTML", layout="wide")
st.title("Гуманизация для читабельности и HTML + AI Score (Ryne)")

st.caption(
    "⚠️ Этичное использование: приложение не предназначено для обхода AI-детекторов. "
    "Оценка AI-следов выполняется через официальный API Ryne, а улучшение текста — локально для читабельности."
)


# =============== Helpers ===============
def read_uploaded_text(file) -> Tuple[str, Optional[str]]:
    """Возвращает текст и тип ('html' или 'text') из загруженного файла."""
    if file is None:
        return "", None
    name = file.name.lower()
    if name.endswith((".html", ".htm")):
        data = file.read().decode("utf-8", errors="ignore")
        return data, "html"
    if name.endswith(".txt") or name.endswith(".md"):
        data = file.read().decode("utf-8", errors="ignore")
        return data, "text"
    if name.endswith(".docx"):
        doc = Document(file)
        text = "\n".join(p.text for p in doc.paragraphs)
        return text, "text"
    # .doc не поддерживаем (нужны системные конвертеры)
    return "", None


def html_to_visible_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text("\n")
    except Exception:
        return html


def improve_readability(text: str) -> str:
    """Простая локальная переработка для читабельности (без обхода детекторов)."""
    # 1) пробелы
    t = re.sub(r"[ \t]+", " ", text)
    # 2) нормализация тире
    t = t.replace(" - ", " — ")
    # 3) разбиение длинных предложений
    sentences = re.split(r"(?<=[.!?])\s+", t)
    lines = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(s) > 240:
            parts = re.split(r",\s+", s)
            buf, cur = [], 0
            for p in parts:
                if cur + len(p) > 120:
                    lines.append(", ".join(buf).strip() + ".")
                    buf, cur = [p], len(p)
                else:
                    buf.append(p)
                    cur += len(p)
            if buf:
                lines.append(", ".join(buf).strip() + ".")
        else:
            lines.append(s)
    t = "\n".join(lines)
    # 4) лишние пустые строки
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def text_to_html(text: str) -> str:
    soup = BeautifulSoup("", "html.parser")
    root = soup.new_tag("div", **{"class": "ryne-output"})
    for para in text.split("\n"):
        if not para.strip():
            continue
        p = soup.new_tag("p")
        p.string = para.strip()
        root.append(p)
    return str(root)


def call_ryne_ai_score(text: str, user_id_api_key: str) -> dict:
    """Официальный публичный эндпоинт Ryne для оценки AI-следов."""
    url = "https://ryne.ai/api/ai-score"
    headers = {"Content-Type": "application/json"}
    payload = {"text": text, "user_id": user_id_api_key}
    resp = requests.post(url, headers=headers, json=payload, timeout=60)

    # Диагностика
    with st.expander("Диагностика ответа (AI Score)"):
        st.write(
            {
                "status": resp.status_code,
                "headers": dict(resp.headers),
                "preview": resp.text[:1200],
            }
        )

    resp.raise_for_status()
    return resp.json()


# =============== Layout ===============
left, right = st.columns([2.2, 1.0])

with left:
    st.subheader("Вставьте текст или HTML")
    input_text = st.text_area(
        "Ваш текст или HTML…", height=300, placeholder="Вставьте сюда текст/HTML…"
    )
    st.caption("…или загрузите файл (.html, .htm, .txt, .md, .docx)")
    up_file = st.file_uploader(
        "Drag & drop / Browse", type=["html", "htm", "txt", "md", "docx"]
    )

uploaded_text, uploaded_kind = ("", None)
if up_file is not None:
    uploaded_text, uploaded_kind = read_uploaded_text(up_file)

source_text = uploaded_text or input_text

with right:
    st.subheader("Выходной формат для локальной переработки")
    out_fmt = st.radio("Формат выдачи", options=["HTML", "Plain/Markdown"], index=0)
    download_as = st.selectbox("Скачать как", options=["HTML", "TXT", "MD"], index=0)


# =============== Ryne AI Score ===============
st.markdown("---")
st.header("🔎 Проверка AI-следов через Ryne (официальный /api/ai-score)")
api_key_body = st.text_input(
    "Ryne API key (user_id)",
    type="password",
    help="Ключ передаётся в теле запроса как user_id.",
)
col_a, col_b = st.columns([1, 4])
with col_a:
    run_score = st.button("Проверить AI-score", type="primary")

if run_score:
    if not source_text.strip():
        st.warning("Нужно вставить текст/HTML или загрузить файл.")
    elif not api_key_body.strip():
        st.warning("Нужен Ryne API key (user_id).")
    else:
        # Если на входе HTML — оцениваем видимый текст
        text_for_check = (
            html_to_visible_text(source_text)
            if ("<" in source_text and ">" in source_text)
            else source_text
        )
        try:
            data = call_ryne_ai_score(text_for_check, api_key_body)
            st.success("Оценка получена")

            # Красивый вывод сводки
            ai_score = data.get("aiScore")
            classification = data.get("classification")
            details = data.get("details") or {}
            analysis = details.get("analysis") or {}
            sentences = details.get("sentences") or []

            cols = st.columns(3)
            cols[0].metric("aiScore", ai_score)
            cols[1].metric("classification", classification)
            cols[2].metric("risk", analysis.get("risk"))

            if analysis:
                st.info(f"Рекомендация: {analysis.get('suggestion', '—')}")

            if sentences:
                st.subheader("По предложениям:")
                for s in sentences:
                    st.write(
                        f"• **{s.get('text','')}** → aiProbability: {s.get('aiProbability')}, isAI: {s.get('isAI')}"
                    )
        except Exception as e:
            st.error(f"Ошибка при обращении к Ryne: {e}")


# =============== Local readability/HTML ===============
st.markdown("---")
st.header("✨ Улучшение читабельности и HTML (локально)")
run_local = st.button("Переписать для читабельности + HTML")

if run_local:
    if not source_text.strip():
        st.warning("Нужно вставить текст/HTML или загрузить файл.")
    else:
        # Если пришёл HTML — извлекаем видимый текст и улучшаем его.
        base_text = (
            html_to_visible_text(source_text)
            if ("<" in source_text and ">" in source_text)
            else source_text
        )
        improved = improve_readability(base_text)

        if out_fmt == "HTML":
            final_html = text_to_html(improved)
            st.success("Готово. Ниже предпросмотр HTML.")
            st.components.v1.html(final_html, height=420, scrolling=True)
            # Скачивание
            st.download_button(
                "⬇️ Скачать HTML",
                data=final_html.encode("utf-8"),
                file_name="result.html",
                mime="text/html",
            )
            st.download_button(
                "⬇️ Скачать TXT",
                data=improved.encode("utf-8"),
                file_name="result.txt",
                mime="text/plain",
            )
        else:
            st.success("Готово. Ниже результат (Plain/Markdown).")
            st.text_area("Результат", value=improved, height=300)
            # Скачивание
            st.download_button(
                "⬇️ Скачать TXT",
                data=improved.encode("utf-8"),
                file_name="result.txt",
                mime="text/plain",
            )
            st.download_button(
                "⬇️ Скачать MD",
                data=improved.encode("utf-8"),
                file_name="result.md",
                mime="text/markdown",
            )
