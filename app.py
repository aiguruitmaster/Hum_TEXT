import os
import io
import json
from typing import Optional, Tuple

import streamlit as st
import requests

from bs4 import BeautifulSoup  # html обработка
from docx import Document      # чтение .docx

# ---------------------------
# Настройки страницы
# ---------------------------
st.set_page_config(page_title="Ryne Humanizer — текст/HTML", layout="wide")

st.title("Гуманизация текста и HTML (через Ryne)")

# --- Сайдбар: настройки API ---
with st.sidebar:
    st.header("API Ryne")
    st.caption("Эти поля нужны, чтобы приложение могло дернуть ваш Ryne-эндпоинт.")
    api_base = st.text_input("API Base URL", value=os.environ.get("RYNE_API_BASE", "https://ryne.ai"))
    endpoint = st.text_input("Endpoint path", value=os.environ.get("RYNE_HUMANIZE_PATH", "/humanize"))
    api_key = st.text_input("API Key (Bearer)", value=os.environ.get("RYNE_API_KEY", ""), type="password")
    st.caption("⚠️ Параметры тела запроса ниже — пример. Проверь у Ryne фактический формат.")
    req_text_field = st.text_input("JSON-поле для текста", value=os.environ.get("RYNE_TEXT_FIELD", "text"))
    req_format_field = st.text_input("JSON-поле для формата", value=os.environ.get("RYNE_FORMAT_FIELD", "format"))
    req_extra_json = st.text_area(
        "Доп. JSON-поля (опционально)", 
        value=os.environ.get("RYNE_EXTRA_JSON", ""),
        placeholder='Напр.: {"temperature": 0.3, "style": "neutral"}'
    )

# --- Колонки как на скриншоте ---
col_left, col_right = st.columns([2.2, 1.0])

# ---------------------------
# Ввод: текст/HTML или файл
# ---------------------------
with col_left:
    st.subheader("Вставьте текст или HTML")
    input_text = st.text_area(
        "Вставьте сюда ваш текст / HTML. Результат вернёт Ryne (/humanize).",
        height=300,
        placeholder="Ваш текст или HTML…",
    )

    st.caption("…или загрузите файл (.html, .htm, .txt, .md, .docx)")
    up_file = st.file_uploader("Drag & drop / Browse", type=["html", "htm", "txt", "md", "docx"])

def read_uploaded_text(file) -> Tuple[str, Optional[str]]:
    """Возвращает (текст, детектированный_тип) из загруженного файла."""
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
    # на .doc лучше не рассчитывать — офлайн-конвертеров без системных зависимостей нет
    return "", None

uploaded_text = ""
uploaded_kind = None
if up_file is not None:
    uploaded_text, uploaded_kind = read_uploaded_text(up_file)

# если пользователь и вставил текст, и загрузил файл — приоритет у файла
source_text = uploaded_text or input_text

# ---------------------------
# Настройки выдачи
# ---------------------------
with col_right:
    st.subheader("Выходной формат")
    out_fmt = st.radio("Формат выдачи", options=["HTML", "Plain/Markdown"], index=0)
    download_as = st.selectbox("Скачать текст как", options=["TXT", "HTML", "MD"])
    run = st.button("🚀 Запустить гуманизацию (Ryne)", type="primary")

# ---------------------------
# Вызов Ryne
# ---------------------------
def call_ryne_humanize(text: str, output_format: str) -> str:
    """
    Шаблон запроса к Ryne.
    !!! Проверь фактическую спецификацию у Ryne и поправь payload/headers !!!
    """
    if not api_base or not endpoint:
        raise RuntimeError("Не задан API Base URL или endpoint.")

    url = api_base.rstrip("/") + "/" + endpoint.lstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {req_text_field: text, req_format_field: ("html" if output_format == "HTML" else "text")}
    # Доп. поля, если заданы
    if req_extra_json.strip():
        try:
            payload.update(json.loads(req_extra_json))
        except Exception as e:
            st.warning(f"Не удалось распарсить Доп. JSON-поля: {e}")

    resp = requests.post(url, json=payload, timeout=60, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Ryne вернул {resp.status_code}: {resp.text[:500]}")
    # Предположим, что Ryne отдаёт ПЛОСКИЙ текст (string).
    # Если приходит JSON — подстройся (например, resp.json()['result'])
    try:
        # если вдруг пришёл JSON с ключом result
        j = resp.json()
        if isinstance(j, dict) and "result" in j:
            return str(j["result"])
        # или если сразу text
        if isinstance(j, dict) and "text" in j:
            return str(j["text"])
        # если неожиданная структура — вернём как строку
        return json.dumps(j, ensure_ascii=False)
    except Exception:
        return resp.text

# ---------------------------
# Преобразование HTML (опционально)
# ---------------------------
def wrap_as_html(text: str) -> str:
    """Если пользователь хочет HTML, но пришёл обычный текст — оборачиваем в простой HTML."""
    soup = BeautifulSoup("", "html.parser")
    body = soup.new_tag("div")
    for para in text.split("\n"):
        p = soup.new_tag("p")
        p.string = para.strip()
        body.append(p)
    return str(body)

def replace_text_nodes_keep_tags(html: str, new_text: str) -> str:
    """
    Вариант: если на входе HTML, а Ryne вернул plain-текст такого же объёма — 
    можно просто заменить весь текст (упрощение).
    Более продвинутый обход с разбором всех текстовых узлов можно добавить при желании.
    """
    # По умолчанию — оборачиваем как блок <div> с новым текстом
    return wrap_as_html(new_text)

# ---------------------------
# Основной запуск
# ---------------------------
if run:
    if not source_text.strip():
        st.warning("Нужно вставить текст/HTML или загрузить файл.")
    else:
        with st.spinner("Обрабатываю через Ryne…"):
            try:
                result = call_ryne_humanize(source_text, out_fmt)

                # Превью и подготовка к скачиванию
                final_html = None
                final_plain = None

                # Если пользователь запросил HTML:
                if out_fmt == "HTML":
                    # Если исходник был HTML — попытаемся отрисовать как HTML.
                    if (uploaded_kind == "html") or ("<html" in source_text.lower() or "<p" in source_text.lower()):
                        # Если Ryne вернул HTML — покажем как есть, иначе завернём в простой HTML
                        if "<" in result and ">" in result:
                            final_html = result
                        else:
                            final_html = replace_text_nodes_keep_tags(source_text, result)
                    else:
                        # Исходник был текстом: превращаем в простой HTML-блок
                        if "<" in result and ">" in result:
                            final_html = result
                        else:
                            final_html = wrap_as_html(result)

                    st.success("Готово! Ниже предпросмотр HTML.")
                    st.components.v1.html(final_html, height=400, scrolling=True)

                else:
                    # Plain/Markdown выводим как есть
                    final_plain = result
                    st.success("Готово! Ниже результат (Plain/Markdown).")
                    st.text_area("Результат", value=final_plain, height=300)

                # Кнопка «Скачать»
                fname = "result"
                if download_as == "HTML":
                    data = (final_html or wrap_as_html(final_plain or "")).encode("utf-8")
                    st.download_button("⬇️ Скачать HTML", data=data, file_name=f"{fname}.html", mime="text/html")
                elif download_as == "MD":
                    data = (final_plain or "").encode("utf-8")
                    st.download_button("⬇️ Скачать MD", data=data, file_name=f"{fname}.md", mime="text/markdown")
                else:
                    data = (final_plain or BeautifulSoup(final_html or "", "html.parser").get_text()).encode("utf-8")
                    st.download_button("⬇️ Скачать TXT", data=data, file_name=f"{fname}.txt", mime="text/plain")

            except Exception as e:
                st.error(f"Ошибка при обращении к Ryne: {e}")
                st.stop()

# Подпись/подсказки
st.caption("Примечание: приложение демонстрационное. Уточни фактический формат API у Ryne и подправь поля запроса в сайдбаре.")
