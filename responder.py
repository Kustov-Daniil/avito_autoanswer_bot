# responder.py
import os
import json
import logging
import difflib
import httpx
from openai import AsyncOpenAI

from config import (
    LLM_MODEL, TEMPERATURE, OPENAI_API_KEY,
    DATA_DIR, FAQ_PATH, STATIC_CONTEXT_PATH, CHAT_HISTORY_PATH,
    SIGNAL_PHRASES,
)
from prompts import build_prompt

logger = logging.getLogger(__name__)
# Create httpx client explicitly to avoid proxy-related issues
http_client = httpx.AsyncClient()
client = AsyncOpenAI(api_key=OPENAI_API_KEY, http_client=http_client)

# Инициализация файлов/папок
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(FAQ_PATH):
    with open(FAQ_PATH, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False)
if not os.path.exists(STATIC_CONTEXT_PATH):
    with open(STATIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
        f.write("")
if not os.path.exists(CHAT_HISTORY_PATH):
    with open(CHAT_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False)

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _build_faq_context(incoming_text: str, faq_data: list) -> str:
    questions = [item.get("question", "") for item in faq_data]
    answers = [item.get("answer", "") for item in faq_data]
    mq = difflib.get_close_matches(incoming_text, questions, n=8, cutoff=0.4)
    ma = difflib.get_close_matches(incoming_text, answers, n=8, cutoff=0.4)

    parts = []
    for q in mq:
        a = next((i["answer"] for i in faq_data if i.get("question") == q), None)
        if a:
            parts.append(f"Вопрос: {q}\nОтвет: {a}")
    for a in ma:
        q = next((i["question"] for i in faq_data if i.get("answer") == a), None)
        if q:
            parts.append(f"Вопрос: {q}\nОтвет: {a}")
    return "\n\n".join(parts[:8])

async def generate_reply(
    dialog_id: str,
    incoming_text: str,
    *,
    user_name: str | None = None,
    embedded_history: str = ""
) -> tuple[str, dict]:
    """
    Единая генерация ответа — для Avito и для Telegram.
    Возвращает (answer_text, {"contains_signal_phrase": bool})
    """
    # История
    chat_history = _load_json(CHAT_HISTORY_PATH, {})
    dialog_history = chat_history.get(dialog_id, [])
    dialog_history.append({"role": "user", "content": incoming_text})
    dialog_history = dialog_history[-10:]
    chat_history[dialog_id] = dialog_history

    # Данные
    faq_data = _load_json(FAQ_PATH, [])
    try:
        with open(STATIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
            static_context = f.read().strip()
    except Exception:
        static_context = ""

    faq_context = _build_faq_context(incoming_text, faq_data)
    dialogue_context = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in dialog_history])

    # Промпт
    prompt = build_prompt(
        static_context=static_context,
        dialogue_context=dialogue_context,
        faq_context=faq_context,
        embedded_history=embedded_history,
        user_name=user_name,
        incoming_text=incoming_text,
    )

    # LLM
    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=TEMPERATURE,
        )
        answer = response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("LLM error: %s", e)
        answer = "Произошла ошибка при получении ответа. Попробуйте позже."

    # Сохраняем ответ ассистента
    dialog_history.append({"role": "assistant", "content": answer})
    chat_history[dialog_id] = dialog_history[-10:]
    _save_json(CHAT_HISTORY_PATH, chat_history)

    # Сигнальная фраза
    lower = answer.lower()
    contains_signal = any(p in lower for p in SIGNAL_PHRASES)
    return answer, {"contains_signal_phrase": contains_signal}
