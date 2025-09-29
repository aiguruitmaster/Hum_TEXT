import json
import re
from typing import Tuple, Optional, Dict, Any, List

import streamlit as st
import requests
from bs4 import BeautifulSoup
from docx import Document


# =========================
# Page config
# =========================
st.set_page_config(page_title="Ryne Humanizer → AI Score", layout="wide")
st.title("Ryne: Хуманизация текста → проверка AI-score")
st.caption(
    "Используйте этично. Хуманизация — для улучшения читабельности/стиля. "
    "Проверка AI-следов выполняется через официальный Ryne API."
)

# =========================
# Secrets / Config
# =========================
RYNE_USER_ID = st.secrets.get("RYNE_USER_ID", "")
RYNE_API_BASE = st.secrets.get("RYNE_API_BASE", "https://ryne.ai")
RYNE_HUMANIZER_PATH = st.secrets.get("RYNE_HUMANIZER_PATH", "/api/humanizer/models/supernova")
RYNE_AI_SCORE_PATH = st.secrets.get("RYNE_AI_SCORE_PATH", "/api/ai-score")

HUMANIZER_URL = RYNE_API_BASE.rstrip("/") + RYNE_HUMANIZER_PATH
AI_SCORE_URL = RYNE_API_BASE.rstrip("/") + RYNE_AI_SCORE_PATH

if not RYNE_USER_ID:
    st.warning(
        "В secrets не найден RYNE_USER_ID. "
        "Создайте .streamlit/secrets.toml и положите туда ваш ключ, см. инструкцию ниже."
    )

# =========================
# Helpers
# =========================
def read_uploaded_text(file) -> Tuple[str, Optional[str]]:
    """Вернёт (содержимое, тип['html'|'text']) из загруженного файла."""
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
    return "", None


def visible_text_from_html(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text("\n")
    except Exception:
        return html


def text_to_html_paragraphs(text: str) -> str:
    """Превратить плоский текст в простой HTML-блок <div><p>...</p>...</div>."""
    soup = BeautifulSoup("", "html.parser")
    root = soup.new_tag("div", **{"class": "ryne-output"})
    for para in (text or "").split("\n"):
        if not para.strip():
            continue
        p = soup.new_tag("p")
        p.string = para.strip()
        root.append(p)
    return str(root)


# =========================
# Ryne API calls
# =========================
def call_ryne_humanize(
    text: str,
    tone: str,
    purpose: str,
    language: str,
    beast_mode: bool,
    preserve_quotes: bool,
    synonym_variation: int,
    streaming: bool,
) -> str:
    """
    Humanizer:
      POST {RYNE_API_BASE}/api/humanizer/models/supernova
      body: {
        text, tone, purpose, language, beast_mode, shouldStream, user_id,
        settings: { preserveQuotes, synonymVariation }
      }

    В non-streaming ждём JSON { content: "..." }.
    В streaming получаем NDJSON-стрим: строки JSON с полями { index, paraphrased }.
    """
    payload: Dict[str, Any] = {
        "text": text,
        "tone": tone,
        "purpose": purpose,
        "language": language,
        "beast_mode": bool(beast_mode),
        "shouldStream": bool(streaming),
        "user_id": RYNE_USER_ID,
        "settings": {
            "preserveQuotes": bool(preserve_quotes),
            "synonymVariation": int(synonym_variation),
        },
    }
    headers = {"Content-Type": "application/json"}

    if not streaming:
        resp = requests.post(HUMANIZER_URL, json=payload, headers=headers, timeout=120)
        with st.expander("Диагностика (Humanizer non-streaming)"):
            st.write({"status": resp.status_code, "preview": resp.text[:1200]})
        resp.raise_for_status()
        data = resp.json()
        # По примеру: .content
        return str(data.get("content", ""))

    # Streaming
    resp = requests.post(HUMANIZER_URL, json=payload, headers=headers, timeout=300, stream=True)
    with st.expander("Диагностика (Humanizer streaming)"):
        st.write({"status": resp.status_code, "headers": dict(resp.headers)})

    resp.raise_for_status()

    chunks: Dict[int, str] = {}
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line:
            continue
        # Попытка распарсить строку как JSON
        try:
            obj = json.loads(line)
        except Exception:
            # иногда сервер может присылать служебные строки
            continue
        if isinstance(obj, dict) and "paraphrased" in obj and isinstance(obj.get("index"), int):
            chunks[obj["index"]] = obj["paraphrased"]

    # Склеиваем по индексу
    result = "".join(v for _, v in sorted(chunks.items(), key=lambda kv: kv[0]))
    return result


def call_ryne_ai_score(text: str) -> Dict[str, Any]:
    """
    AI Score:
      POST {RYNE_API_BASE}/api/ai-score
      body: { text, user_id }
    """
    payload = {"text": text, "user_id": RYNE_USER_ID}
    headers = {"Content-Type": "application/json"}
    resp = requests.post(AI_SCORE_URL, json=payload, headers=headers, timeout=90)
    with st.expander("Диагностика (AI-score)"):
        st.write({"status": resp.status_code, "preview": resp.text[:1200]})
    resp.raise_for_status()
    return resp.json()


# =========================
# UI — Input
# =========================
left, right = st.columns([2.2, 1.0])

with left:
    st.subheader("Вставьте текст или загрузите файл")
    input_text = st.text_area("Текст или HTML…", height=260, placeholder="Вставьте сюда исходный текст…")
    st.caption("…или загрузите файл (.html, .htm, .txt, .md, .docx)")
    up_file = st.file_uploader("Загрузить файл", type=["html", "htm", "txt", "md", "docx"])

uploaded_text, uploaded_kind = ("", None)
if up_file is not None:
    uploaded_text, uploaded_kind = read_uploaded_text(up_file)

source_text = uploaded_text or input_text
if not source_text.strip():
    st.info("Подсказка: введите текст в поле слева или загрузите файл.")

with right:
    st.subheader("Humanizer настройки")
    tone = st.selectbox("Tone", ["professional", "conversational", "neutral", "friendly", "persuasive"], index=0)
    purpose = st.selectbox("Purpose", ["blog", "article", "email", "essay", "report", "social"], index=0)
    language = st.selectbox("Language", ["english", "ukrainian", "russian", "german", "spanish"], index=0)
    beast_mode = st.checkbox("beast_mode", value=True, help="Включает усиленную переработку")
    preserve_quotes = st.checkbox("settings.preserveQuotes", value=True)
    synonym_var = st.slider("settings.synonymVariation", min_value=0, max_value=100, value=40, step=5)
    streaming = st.toggle("Streaming", value=False, help="Если включено — собираем NDJSON-стрим по частям")
    output_as_html = st.radio("Вывод результата", ["HTML", "Plain"], index=0)

st.markdown("---")

# =========================
# Actions
# =========================
col1, col2, col3 = st.columns([1.2, 1.2, 2])

if "humanized_text" not in st.session_state:
    st.session_state["humanized_text"] = ""

with col1:
    btn_humanize = st.button("🚀 Humanize")
with col2:
    btn_humanize_then_check = st.button("🚀 Humanize → 🔎 Check")
with col3:
    btn_check_only = st.button("🔎 Check current text")

# =========================
# Run — Humanize
# =========================
def run_humanize_flow(src_text: str) -> str:
    if not RYNE_USER_ID:
        st.error("Нет RYNE_USER_ID в secrets. Добавьте ключ и перезапустите приложение.")
        st.stop()
    if not src_text.strip():
        st.warning("Нужно вставить исходный текст/HTML или загрузить файл.")
        st.stop()

    # Если на входе HTML — Ryne Humanizer ждёт обычный текст. Возьмём видимую часть.
    text_for_humanizer = visible_text_from_html(src_text) if ("<" in src_text and ">" in src_text) else src_text

    with st.spinner("Отправляю в Ryne Humanizer…"):
        try:
            result = call_ryne_humanize(
                text=text_for_humanizer,
                tone=tone,
                purpose=purpose,
                language=language,
                beast_mode=beast_mode,
                preserve_quotes=preserve_quotes,
                synonym_variation=synonym_var,
                streaming=streaming,
            )
        except Exception as e:
            st.error(f"Ошибка Humanizer: {e}")
            st.stop()

    st.session_state["humanized_text"] = result or ""
    return st.session_state["humanized_text"]


def render_output(text: str):
    if not text:
        st.info("Пока нет результата.")
        return
    st.success("Готово! Результат ниже.")
    if output_as_html == "HTML":
        html = text_to_html_paragraphs(text)
        st.components.v1.html(html, height=420, scrolling=True)
        st.download_button("⬇️ Скачать HTML", data=html.encode("utf-8"), file_name="humanized.html", mime="text/html")
    else:
        st.text_area("Результат (Plain)", value=text, height=300)
        st.download_button("⬇️ Скачать TXT", data=text.encode("utf-8"), file_name="humanized.txt", mime="text/plain")


# =========================
# Run — Check AI score
# =========================
def run_check_flow(text_to_check: str):
    if not RYNE_USER_ID:
        st.error("Нет RYNE_USER_ID в secrets.")
        st.stop()
    if not text_to_check.strip():
        st.warning("Нет текста для проверки.")
        st.stop()

    with st.spinner("Запрашиваю Ryne AI-score…"):
        try:
            data = call_ryne_ai_score(text_to_check)
        except Exception as e:
            st.error(f"Ошибка AI-score: {e}")
            st.stop()

    ai_score = data.get("aiScore")
    classification = data.get("classification")
    details = data.get("details") or {}
    analysis = details.get("analysis") or {}
    sentences: List[Dict[str, Any]] = details.get("sentences") or []

    c1, c2, c3 = st.columns(3)
    c1.metric("aiScore", ai_score)
    c2.metric("classification", classification)
    c3.metric("risk", analysis.get("risk"))

    if analysis:
        st.info(f"Рекомендация: {analysis.get('suggestion', '—')}")

    if sentences:
        st.subheader("По предложениям:")
        for s in sentences:
            st.write(f"• **{s.get('text','')}** → aiProbability: {s.get('aiProbability')}, isAI: {s.get('isAI')}")


# =========================
# Buttons logic
# =========================
if btn_humanize:
    result = run_humanize_flow(source_text)
    render_output(result)

if btn_check_only:
    # Проверяем текущий текст: приоритет — humanized, иначе — исходный
    candidate = st.session_state.get("humanized_text") or (
        visible_text_from_html(source_text) if ("<" in source_text and ">" in source_text) else source_text
    )
    run_check_flow(candidate)

if btn_humanize_then_check:
    result = run_humanize_flow(source_text)
    render_output(result)
    st.markdown("---")
    st.header("🔎 Проверка результата (AI-score)")
    run_check_flow(result)

# =========================
# Secrets how-to
# =========================
with st.expander("Как настроить Streamlit Secrets"):
    st.markdown(
        """

