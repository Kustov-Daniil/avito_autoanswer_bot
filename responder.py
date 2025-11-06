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
MAX_HISTORY_MESSAGES: int = 6
MAX_FAQ_MATCHES: int = 3
FAQ_SIMILARITY_CUTOFF: float = 0.55

# Инициализация OpenAI клиента
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY not set! LLM functionality will not work.")
    http_client = None
    client = None
else:
    try:
        http_client = httpx.AsyncClient()
        client = AsyncOpenAI(api_key=OPENAI_API_KEY, http_client=http_client)
        logger.info("OpenAI client initialized successfully with model=%s", LLM_MODEL)
    except Exception as e:
        logger.exception("Failed to initialize OpenAI client: %s", e)
        http_client = None
        client = None

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
) -> Tuple[Optional[str], Dict[str, bool]]:
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
        - answer_text: Сгенерированный ответ или None если произошла ошибка (не отправляется клиенту)
        - contains_signal_phrase: True если ответ содержит сигнальную фразу о передаче менеджеру
    """
    if not incoming_text or not incoming_text.strip():
        logger.warning("generate_reply called with empty incoming_text")
        return "Извините, не получилось обработать ваше сообщение. Попробуйте еще раз.", {"contains_signal_phrase": False}
    
    # Загружаем историю диалога
    chat_history = _load_json(CHAT_HISTORY_PATH, {})
    dialog_history = chat_history.get(dialog_id, [])
    
    logger.info(
        "Loaded chat history for dialog_id=%s: %d messages",
        dialog_id, len(dialog_history)
    )
    
    # Для контекста используем историю БЕЗ нового сообщения пользователя
    # (новое сообщение передается отдельно в incoming_text)
    # Ограничиваем историю последними N сообщениями (исключая новое сообщение)
    dialog_history_for_context = dialog_history[-MAX_HISTORY_MESSAGES:] if dialog_history else []
    
    # Добавляем новое сообщение пользователя в историю (для сохранения)
    # Но НЕ сохраняем ответ ассистента здесь - это будет сделано в main.py после успешной отправки
    dialog_history.append({"role": "user", "content": incoming_text})
    
    # Сохраняем сообщение пользователя в историю
    chat_history[dialog_id] = dialog_history[-MAX_HISTORY_MESSAGES:]
    _save_json(CHAT_HISTORY_PATH, chat_history)
    
    logger.info(
        "Saved user message to chat history for dialog_id=%s: %d messages",
        dialog_id, len(chat_history[dialog_id])
    )
    
    # Загружаем FAQ и статический контекст
    faq_data = _load_json(FAQ_PATH, [])
    
    try:
        with open(STATIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
            static_context = f.read().strip()
    except (FileNotFoundError, IOError) as e:
        logger.warning("Failed to load static context: %s", e)
        static_context = ""
    
    # Строим контексты - используем историю БЕЗ нового сообщения пользователя для контекста
    # (новое сообщение передается отдельно в incoming_text)
    faq_context = _build_faq_context(incoming_text, faq_data)
    dialogue_context = "\n".join([
        f"{m['role'].capitalize()}: {m['content']}"
        for m in dialog_history_for_context
        if m.get("role") and m.get("content")
    ])
    
    logger.info(
        "Built dialogue_context for dialog_id=%s: %d messages, context_length=%d",
        dialog_id, len(dialog_history_for_context), len(dialogue_context)
    )
    
    # Формируем промпт
    prompt = build_prompt(
        static_context=static_context,
        dialogue_context=dialogue_context,
        faq_context=faq_context,
        embedded_history=embedded_history,
        user_name=user_name,
        incoming_text=incoming_text,
    )
    
    logger.info(
        "Calling LLM for dialog_id=%s, model=%s, prompt_length=%d",
        dialog_id, LLM_MODEL, len(prompt)
    )
    
    # Проверяем, что клиент инициализирован
    if not client:
        logger.error("OpenAI client not initialized! Cannot generate reply for dialog_id=%s", dialog_id)
        return None, {"contains_signal_phrase": True}
    
    # Генерируем ответ через LLM
    try:
        # Проверяем, поддерживает ли модель temperature
        # Для gpt-5-mini и некоторых других моделей temperature не поддерживается
        use_temperature = LLM_MODEL not in ["gpt-5-mini", "gpt-5"]
        
        logger.info(
            "Creating chat completion: model=%s, use_temperature=%s, temperature=%s",
            LLM_MODEL, use_temperature, TEMPERATURE if use_temperature else "N/A"
        )
        
        if use_temperature:
            response = await client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=TEMPERATURE,
            )
        else:
            response = await client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
        
        logger.info("LLM response received: model=%s, choices=%d", response.model, len(response.choices))
        
        if not response.choices or not response.choices[0].message:
            logger.error("LLM response has no choices or message for dialog_id=%s", dialog_id)
            return None, {"contains_signal_phrase": True}
        
        answer = response.choices[0].message.content
        if answer:
            answer = answer.strip()
        else:
            answer = ""
        
        logger.info("LLM answer extracted: length=%d, preview=%s", len(answer), answer[:100] if answer else "EMPTY")
        
        if not answer:
            logger.warning("LLM returned empty answer for dialog_id=%s", dialog_id)
            # При ошибке переводим на менеджера
            return None, {"contains_signal_phrase": True}
    except Exception as e:
        logger.exception("LLM error for dialog_id=%s: %s", dialog_id, e)
        logger.error("Full error details: type=%s, args=%s", type(e).__name__, e.args)
        # При ошибке переводим на менеджера
        return None, {"contains_signal_phrase": True}
    
    # Проверяем наличие сигнальной фразы (для внутренней логики)
    answer_lower = answer.lower()
    contains_signal = any(phrase in answer_lower for phrase in SIGNAL_PHRASES)
    
    # Если обнаружена сигнальная фраза - заменяем весь ответ на сообщение для клиента
    # Клиент не должен знать, что его передают менеджеру
    if contains_signal:
        logger.info("Generated reply contains signal phrase for dialog_id=%s, replacing with client message", dialog_id)
        # Заменяем весь ответ на сообщение для клиента
        answer = "Подождите, пожалуйста, уточняю информацию"
    
    # НЕ сохраняем ответ в историю здесь - это будет сделано в main.py после успешной отправки
    # Это нужно, чтобы не сохранять ответы, которые не были отправлены клиенту
    
    return answer, {"contains_signal_phrase": contains_signal}
