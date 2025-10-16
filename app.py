import json
import re
import io
from typing import List, Dict, Any, Tuple, Optional

import streamlit as st
from bs4 import BeautifulSoup

try:
    from docx import Document
except Exception:
    Document = None

# ============ Streamlit UI ============
st.set_page_config(page_title="SEO Humanizer (Anchors + SerfSEO) → OpenAI + Ryne", layout="wide")
st.title("SEO Humanizer: Anchors + SerfSEO → OpenAI (Streaming) → Ryne AI-score")
st.caption("Промпт зашит в код. Ключи OpenAI и Ryne хранятся в secrets.")

# -------- Secrets --------
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", "")
OPENAI_MODEL = st.secrets.get("OPENAI_MODEL", "gpt-4o-mini")

RYNE_USER_ID = st.secrets.get("RYNE_USER_ID", "")
RYNE_API_BASE = st.secrets.get("RYNE_API_BASE", "https://ryne.ai")
RYNE_AI_SCORE_PATH = st.secrets.get("RYNE_AI_SCORE_PATH", "/api/ai-score")
AI_SCORE_URL = RYNE_API_BASE.rstrip("/") + RYNE_AI_SCORE_PATH

if not OPENAI_API_KEY:
    st.warning("❗ В secrets отсутствует OPENAI_API_KEY")

if not RYNE_USER_ID:
    st.warning("❗ В secrets отсутствует RYNE_USER_ID")

# -------- Prompt (НЕ МЕНЯТЬ, как прислал) --------
HUMANIZE_PROMPT_TEMPLATE = """# HUMANIZE ≤15% — Contrastive Triple + Fusion (anchors + keyword ranges)

You are a native-level editor for the target locale. Rewrite and humanize the SOURCE_TEXT in the **same language**. Keep meaning and compliance, integrate anchors and keyword ranges naturally, and aim for text that typically scores **≤15% AI-written** in common detectors (no guarantees).

## INPUTS
SOURCE_TEXT: <<paste full original text>>
LANGUAGE: auto-detect; write output in the same language.
BRAND_NAME: <<e.g., LuckyHills>>
GEO & LOCALE: <<e.g., Italy / Italian audience>>
BRAND_FACTS (hard): e.g., “Licensed in Curaçao only; do NOT claim AAMS/ADM or other licenses.”
ANCHORS (use each exactly once; keep anchor text + URL intact; spread early/mid/late):
- "<<anchor_text_1>>" – <<URL_1>>
- "<<anchor_text_2>>" – <<URL_2>>
KEYWORDS_WITH_RANGES (exact-match min–max for the whole doc; some marked for headings):
- "<<keyword_1>>": min–max, heading: yes/no
- ...
KEYWORDS_FOR_HEADINGS: <<list>>
GAME_TITLES_WHITELIST: <<only titles allowed; do not invent new games>>

## STYLE TARGETS (anti-detector)
- Sentence-length mix: ~25% short (≤8 words), ~60% medium (9–18), ~15% long (19–28).
- Vary syntax: fronting, parentheticals (— … — / (…) ), occasional fragments if idiomatic.
- Cap repeated openers: do **not** start two consecutive sentences with the same word.
- Limit generic connectors (e.g., “Inoltre”, “Pertanto”): ≤2 per piece; use varied local alternatives.
- Avoid “AI-scented” words (and local equivalents): Experience, Discover, Explore, Imagine, Looking, Start, Engage, Tailors, Cutting-edge, Tailored, Simplifies, Unleash, Unlock, Dive, Effortlessly, Seamlessly, Tailor, Maximize, Transform, Simplify, Flawless.

## STRUCTURE
- Keep Markdown H1 + multiple H2 (+ H3 as needed).
- **Always include body text after each heading.**
- Add a 1–2 sentence lead-in before every list or table.
- Place heading-marked keywords into H1/H2/H3 where natural.

## LENGTH
- Final length ≥90% of SOURCE_TEXT (you may go up to +20% if it helps clarity/SEO).

## COMPLIANCE
- Respect BRAND_FACTS (e.g., Curaçao only; never imply AAMS/ADM).
- Mention only games from GAME_TITLES_WHITELIST.
- Keep responsible gambling line appropriate to locale (18+, gioco responsabile) if natural.

## WORKFLOW (contrastive → fusion)
1) Produce **3 materially different rewrites** (A/B/C) meeting all constraints above (anchors/keywords/structure/length).
   - Each version must distribute anchors differently (early/mid/late).
   - Ensure keyword counts fall within given ranges in **each** version.
2) **Self-select & fuse**: choose the most natural-sounding version and lightly fuse 1–2 strong sentences from the others for rhythm variety.
3) Output **only the final fused text** in the same language. No notes, no markup fences.

## OUTPUT
Return the final humanized article only (same language), with all anchors clickable and exactly as provided.
"""

# -------- Helpers: parsing inputs --------
def parse_anchors(raw: str) -> List[Tuple[str, str]]:
    """
    Ввод строками:
      Текст анкоры — https://url
    подойдут разделители: "—", "-", " - ", " — "
    """
    anchors = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Разделитель: длинное тире или дефис
        parts = re.split(r"\s+—\s+|\s+-\s+| — | - ", line, maxsplit=1)
        if len(parts) != 2:
            continue
        text, url = parts[0].strip().strip('"“”'), parts[1].strip()
        anchors.append((text, url))
    return anchors

def parse_keywords_ranges(raw: str) -> List[Dict[str, Any]]:
    """
    Форматы:
    1) post per line: keyword | min | max | heading:[yes/no]
    2) JSON: [{"keyword":"...", "min":2, "max":5, "heading":true}, ...]
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    # JSON?
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            out = []
            for o in arr:
                out.append({
                    "keyword": str(o.get("keyword","")).strip(),
                    "min": int(o.get("min", 0)),
                    "max": int(o.get("max", 0)),
                    "heading": bool(o.get("heading", False)),
                })
            return out
        except Exception:
            pass

    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        cols = [c.strip() for c in re.split(r"\s*\|\s*", line)]
        if len(cols) < 3:
            continue
        kw = cols[0]
        try:
            minv = int(cols[1])
            maxv = int(cols[2])
        except Exception:
            continue
        heading = False
        if len(cols) >= 4:
            heading = cols[3].lower() in ("yes", "y", "true", "1", "да")
        out.append({"keyword": kw, "min": minv, "max": maxv, "heading": heading})
    return out

def build_inputs_block(
    source_text: str,
    brand_name: str,
    geo_locale: str,
    brand_facts: str,
    anchors: List[Tuple[str, str]],
    kw_ranges: List[Dict[str, Any]],
    headings_keywords: List[str],
    whitelist_games: List[str],
) -> str:
    # ANCHORS lines
    if anchors:
        anchors_lines = "\n".join([f'- "{a[0]}" – {a[1]}' for a in anchors])
    else:
        anchors_lines = '- "<<anchor_text_1>>" – <<URL_1>>\n- "<<anchor_text_2>>" – <<URL_2>>'

    # KEYWORDS_WITH_RANGES lines
    if kw_ranges:
        def one(k): 
            return f'- "{k["keyword"]}": {k["min"]}–{k["max"]}, heading: {"yes" if k.get("heading") else "no"}'
        kw_lines = "\n".join(one(k) for k in kw_ranges)
    else:
        kw_lines = '- "<<keyword_1>>": min–max, heading: yes/no\n- ...'

    headings_line = ", ".join(headings_keywords) if headings_keywords else "<<list>>"
    whitelist_line = ", ".join(whitelist_games) if whitelist_games else "<<only titles allowed; do not invent new games>>"

    # Вставляем значения в блок ## INPUTS оригинального промпта
    block = f"""## INPUTS
SOURCE_TEXT: {source_text}
LANGUAGE: auto-detect; write output in the same language.
BRAND_NAME: {brand_name or "<<e.g., LuckyHills>>"}
GEO & LOCALE: {geo_locale or "<<e.g., Italy / Italian audience>>"}
BRAND_FACTS (hard): {brand_facts or "e.g., “Licensed in Curaçao only; do NOT claim AAMS/ADM or other licenses.”"}
ANCHORS (use each exactly once; keep anchor text + URL intact; spread early/mid/late):
{anchors_lines}
KEYWORDS_WITH_RANGES (exact-match min–max for the whole doc; some marked for headings):
{kw_lines}
KEYWORDS_FOR_HEADINGS: {headings_line}
GAME_TITLES_WHITELIST: {whitelist_line}
"""
    return block

def merge_prompt_with_inputs(prompt_template: str, inputs_block: str) -> str:
    """
    Подменяем в шаблоне секцию ## INPUTS целиком на сгенерированный блок,
    остальную структуру промпта не трогаем.
    """
    # Найти границы секции ## INPUTS
    pattern = re.compile(r"(## INPUTS)(.*?)(## STYLE TARGETS)", re.DOTALL)
    m = pattern.search(prompt_template)
    if not m:
        # если по какой-то причине не нашли — просто конкатенируем
        return prompt_template + "\n\n" + inputs_block
    start, end = m.span(2)
    new_prompt = prompt_template[:m.start(2)] + "\n" + inputs_block + "\n" + prompt_template[m.end(2):]
    return new_prompt

def to_html(text: str) -> str:
    soup = BeautifulSoup("", "html.parser")
    root = soup.new_tag("div", **{"class": "output"})
    for para in (text or "").split("\n"):
        if not para.strip():
            continue
        p = soup.new_tag("p"); p.string = para.strip(); root.append(p)
    return str(root)

# -------- OpenAI (stream) --------
def call_openai_stream(full_prompt: str) -> str:
    # Новая библиотека openai (>=1.0)
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    stream = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": full_prompt}],
        temperature=0.7,
        stream=True,
    )
    container = st.empty()
    acc = []
    for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        if delta:
            acc.append(delta)
            container.markdown("".join(acc))
    return "".join(acc)

# -------- Ryne AI-score --------
def call_ryne_ai_score(text: str) -> Dict[str, Any]:
    import requests
    payload = {"text": text, "user_id": RYNE_USER_ID}
    headers = {"Content-Type": "application/json"}
    resp = requests.post(AI_SCORE_URL, json=payload, headers=headers, timeout=90)
    with st.expander("Debug (AI-score)"):
        st.write({"status": resp.status_code, "preview": resp.text[:800]})
    resp.raise_for_status()
    return resp.json()

# ======= UI layout =======
left, right = st.columns([2.2, 1.2])

with left:
    st.subheader("SOURCE_TEXT")
    src = st.text_area("Исходный текст", height=260, placeholder="Вставь исходный текст...")

    st.subheader("Anchors")
    st.caption('Формат по строкам: Текст анкоры — https://url')
    anchors_raw = st.text_area("Анкоры (по строкам)", height=140, placeholder='Пример:\n"Лучшие бонусы казино" — https://example.com/bonuses\n"Слоты онлайн" — https://example.com/slots')

    st.subheader("SerfSEO Keywords (с диапазонами)")
    st.caption('Формат 1: keyword | min | max | heading:[yes/no]\nФормат 2: JSON-массив объектов.')
    kw_raw = st.text_area("Ключи (диапазоны)", height=160, placeholder='пример:\nслоты онлайн | 2 | 4 | heading:yes\nбонус казино | 1 | 2 | heading:no')

with right:
    st.subheader("Meta")
    brand_name = st.text_input("BRAND_NAME", "")
    geo_locale = st.text_input("GEO & LOCALE", "")
    brand_facts = st.text_area("BRAND_FACTS (hard)", height=90, placeholder='напр.: “Licensed in Curaçao only; do NOT claim AAMS/ADM or other licenses.”')

    st.subheader("Keywords for Headings")
    headings_list = st.text_input("KEYWORDS_FOR_HEADINGS (через запятую)", "")

    st.subheader("GAME_TITLES_WHITELIST")
    whitelist = st.text_input("Названия (через запятую)", "")

    output_fmt = st.radio("Формат скачивания", ["TXT", "HTML", "DOCX"], index=0)

st.markdown("---")
colA, colB = st.columns([1.2, 1.8])

if "final_text" not in st.session_state:
    st.session_state["final_text"] = ""

with colA:
    run_btn = st.button("🚀 Сгенерировать (Streaming) + 🔎 Ryne AI-score")

with colB:
    check_btn = st.button("🔎 Проверить AI-score для текущего результата")

# ======= Actions =======
def build_full_prompt() -> str:
    anchors = parse_anchors(anchors_raw)
    kw_ranges = parse_keywords_ranges(kw_raw)
    headings_keywords = [s.strip() for s in (headings_list or "").split(",") if s.strip()]
    whitelist_games = [s.strip() for s in (whitelist or "").split(",") if s.strip()]

    inputs_block = build_inputs_block(
        source_text=src or "<<paste full original text>>",
        brand_name=brand_name,
        geo_locale=geo_locale,
        brand_facts=brand_facts,
        anchors=anchors,
        kw_ranges=kw_ranges,
        headings_keywords=headings_keywords,
        whitelist_games=whitelist_games,
    )
    full_prompt = merge_prompt_with_inputs(HUMANIZE_PROMPT_TEMPLATE, inputs_block)
    return full_prompt

def download_render(text: str):
    if output_fmt == "TXT":
        st.download_button("⬇️ Скачать TXT", data=text.encode("utf-8"), file_name="humanized.txt", mime="text/plain")
    elif output_fmt == "HTML":
        html = to_html(text)
        st.components.v1.html(html, height=420, scrolling=True)
        st.download_button("⬇️ Скачать HTML", data=html.encode("utf-8"), file_name="humanized.html", mime="text/html")
    else:  # DOCX
        if Document is None:
            st.error("python-docx не установлен")
            return
        doc = Document()
        for para in (text or "").split("\n"):
            if para.strip():
                doc.add_paragraph(para.strip())
        bio = io.BytesIO()
        doc.save(bio)
        st.download_button("⬇️ Скачать DOCX", data=bio.getvalue(), file_name="humanized.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

def run_generation_and_score():
    if not OPENAI_API_KEY:
        st.error("Нет OPENAI_API_KEY в secrets.")
        st.stop()
    if not src.strip():
        st.warning("Вставь SOURCE_TEXT.")
        st.stop()

    full_prompt = build_full_prompt()

    st.subheader("🧠 OpenAI — streaming предпросмотр")
    try:
        text = call_openai_stream(full_prompt)
    except Exception as e:
        st.error(f"OpenAI error: {e}")
        st.stop()

    st.session_state["final_text"] = text or ""
    st.markdown("---")
    st.subheader("📄 Результат")
    st.text_area("Готовый текст", value=text, height=260)
    download_render(text)

    # AI-score (Ryne)
    st.markdown("---")
    st.subheader("🔎 Ryne AI-score")
    if not RYNE_USER_ID:
        st.error("Нет RYNE_USER_ID в secrets.")
        st.stop()
    try:
        data = call_ryne_ai_score(text)
    except Exception as e:
        st.error(f"AI-score error: {e}")
        st.stop()

    ai_score = data.get("aiScore")
    classification = data.get("classification")
    details = (data.get("details") or {})
    analysis = details.get("analysis") or {}
    sentences = details.get("sentences") or []

    c1, c2, c3 = st.columns(3)
    c1.metric("aiScore", ai_score)
    c2.metric("classification", classification)
    c3.metric("risk", analysis.get("risk"))

    if analysis:
        st.info(f"Suggestion: {analysis.get('suggestion', '-')}")
    if sentences:
        st.write("Пересчёт по предложениям:")
        for s in sentences:
            st.write(f"- {s.get('text','')}\n  → aiProbability: {s.get('aiProbability')}, isAI: {s.get('isAI')}")

def run_score_only():
    text = st.session_state.get("final_text", "")
    if not text.strip():
        st.warning("Нет текста для проверки. Сначала сгенерируй.")
        st.stop()
    if not RYNE_USER_ID:
        st.error("Нет RYNE_USER_ID в secrets.")
        st.stop()
    data = call_ryne_ai_score(text)
    ai_score = data.get("aiScore")
    classification = data.get("classification")
    details = (data.get("details") or {})
    analysis = details.get("analysis") or {}
    sentences = details.get("sentences") or []

    c1, c2, c3 = st.columns(3)
    c1.metric("aiScore", ai_score)
    c2.metric("classification", classification)
    c3.metric("risk", analysis.get("risk"))

    if analysis:
        st.info(f"Suggestion: {analysis.get('suggestion', '-')}")
    if sentences:
        st.write("Пересчёт по предложениям:")
        for s in sentences:
            st.write(f"- {s.get('text','')}\n  → aiProbability: {s.get('aiProbability')}, isAI: {s.get('isAI')}")

if run_btn:
    run_generation_and_score()

if check_btn:
    run_score_only()
