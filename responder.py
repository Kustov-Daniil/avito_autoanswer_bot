"""
Модуль генерации ответов на основе LLM.

Единый модуль для генерации ответов как для Avito, так и для Telegram.
Использует FAQ, статический контекст и историю диалога для формирования ответов.
"""
import os
import json
import logging
import difflib
import re
import httpx
from typing import Dict, Any, List, Tuple, Optional
from openai import AsyncOpenAI

from config import (
    LLM_MODEL, TEMPERATURE, OPENAI_API_KEY,
    DATA_DIR, FAQ_PATH, STATIC_CONTEXT_PATH, CHAT_HISTORY_PATH,
    SIGNAL_PHRASES,
)
from prompts import build_prompt

logger = logging.getLogger(__name__)

# Константы
MAX_HISTORY_MESSAGES: int = 10
MAX_FAQ_MATCHES: int = 8
FAQ_SIMILARITY_CUTOFF: float = 0.4

# Инициализация OpenAI клиента
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


def _load_json(path: str, default: Any) -> Any:
    """
    Безопасная загрузка JSON файла.
    
    Args:
        path: Путь к JSON файлу
        default: Значение по умолчанию при ошибке
        
    Returns:
        Загруженные данные или значение по умолчанию
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError) as e:
        logger.warning("Failed to load JSON from %s: %s, using default", path, e)
        return default


def _save_json(path: str, data: Any) -> None:
    """
    Безопасное сохранение данных в JSON файл.
    
    Args:
        path: Путь к JSON файлу
        data: Данные для сохранения
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except (IOError, OSError) as e:
        logger.error("Failed to save JSON to %s: %s", path, e)
        raise


def _build_faq_context(incoming_text: str, faq_data: List[Dict[str, str]]) -> str:
    """
    Строит контекст из FAQ на основе схожести с входящим текстом.
    
    Args:
        incoming_text: Входящий текст пользователя
        faq_data: Список FAQ записей [{"question": str, "answer": str}]
        
    Returns:
        Строка с релевантными вопросами и ответами из FAQ
    """
    if not faq_data or not incoming_text:
        return ""
    
    questions = [item.get("question", "") for item in faq_data if item.get("question")]
    answers = [item.get("answer", "") for item in faq_data if item.get("answer")]
    
    # Находим похожие вопросы
    matched_questions = difflib.get_close_matches(
        incoming_text, questions, n=MAX_FAQ_MATCHES, cutoff=FAQ_SIMILARITY_CUTOFF
    )
    
    # Находим похожие ответы
    matched_answers = difflib.get_close_matches(
        incoming_text, answers, n=MAX_FAQ_MATCHES, cutoff=FAQ_SIMILARITY_CUTOFF
    )
    
    parts = []
    seen = set()
    
    # Добавляем вопросы и ответы
    for q in matched_questions:
        if q not in seen:
            a = next((i["answer"] for i in faq_data if i.get("question") == q), None)
            if a:
                parts.append(f"Вопрос: {q}\nОтвет: {a}")
                seen.add(q)
    
    for a in matched_answers:
        if a not in seen:
            q = next((i["question"] for i in faq_data if i.get("answer") == a), None)
            if q:
                parts.append(f"Вопрос: {q}\nОтвет: {a}")
                seen.add(a)
    
    return "\n\n".join(parts[:MAX_FAQ_MATCHES])


async def generate_reply(
    dialog_id: str,
    incoming_text: str,
    *,
    user_name: Optional[str] = None,
    embedded_history: str = ""
) -> Tuple[str, Dict[str, bool]]:
    """
    Генерирует ответ на основе входящего текста, FAQ и истории диалога.
    
    Единая генерация ответа для Avito и для Telegram.
    
    Args:
        dialog_id: Уникальный ID диалога (например, "avito_123" или "tg_456")
        incoming_text: Входящий текст от пользователя
        user_name: Имя пользователя (опционально)
        embedded_history: Вложенная история диалога (опционально)
        
    Returns:
        Кортеж (answer_text, {"contains_signal_phrase": bool})
        - answer_text: Сгенерированный ответ
        - contains_signal_phrase: True если ответ содержит сигнальную фразу о передаче менеджеру
    """
    if not incoming_text or not incoming_text.strip():
        logger.warning("generate_reply called with empty incoming_text")
        return "Извините, не получилось обработать ваше сообщение. Попробуйте еще раз.", {"contains_signal_phrase": False}
    
    # Загружаем историю диалога
    chat_history = _load_json(CHAT_HISTORY_PATH, {})
    dialog_history = chat_history.get(dialog_id, [])
    
    # Добавляем новое сообщение пользователя
    dialog_history.append({"role": "user", "content": incoming_text})
    
    # Ограничиваем историю последними N сообщениями
    dialog_history = dialog_history[-MAX_HISTORY_MESSAGES:]
    chat_history[dialog_id] = dialog_history
    
    # Загружаем FAQ и статический контекст
    faq_data = _load_json(FAQ_PATH, [])
    
    try:
        with open(STATIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
            static_context = f.read().strip()
    except (FileNotFoundError, IOError) as e:
        logger.warning("Failed to load static context: %s", e)
        static_context = ""
    
    # Строим контексты
    faq_context = _build_faq_context(incoming_text, faq_data)
    dialogue_context = "\n".join([
        f"{m['role'].capitalize()}: {m['content']}"
        for m in dialog_history
        if m.get("role") and m.get("content")
    ])
    
    # Формируем промпт
    prompt = build_prompt(
        static_context=static_context,
        dialogue_context=dialogue_context,
        faq_context=faq_context,
        embedded_history=embedded_history,
        user_name=user_name,
        incoming_text=incoming_text,
    )
    
    # Генерируем ответ через LLM
    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=TEMPERATURE,
        )
        answer = response.choices[0].message.content.strip()
        
        if not answer:
            logger.warning("LLM returned empty answer")
            answer = "Произошла ошибка при получении ответа. Попробуйте позже."
    except Exception as e:
        logger.exception("LLM error: %s", e)
        answer = "Произошла ошибка при получении ответа. Попробуйте позже."
    
    # Сохраняем ответ ассистента в историю
    dialog_history.append({"role": "assistant", "content": answer})
    chat_history[dialog_id] = dialog_history[-MAX_HISTORY_MESSAGES:]
    _save_json(CHAT_HISTORY_PATH, chat_history)
    
    # Проверяем наличие сигнальной фразы (для внутренней логики)
    answer_lower = answer.lower()
    contains_signal = any(phrase in answer_lower for phrase in SIGNAL_PHRASES)
    
    # Если обнаружена сигнальная фраза - заменяем весь ответ на сообщение для клиента
    # Клиент не должен знать, что его передают менеджеру
    if contains_signal:
        logger.info("Generated reply contains signal phrase for dialog_id=%s, replacing with client message", dialog_id)
        # Заменяем весь ответ на сообщение для клиента
        answer = "Подождите, пожалуйста, уточняю информацию"
    
    return answer, {"contains_signal_phrase": contains_signal}
