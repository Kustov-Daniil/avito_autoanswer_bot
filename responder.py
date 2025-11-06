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
    DATA_DIR, FAQ_PATH, STATIC_CONTEXT_PATH, DYNAMIC_CONTEXT_PATH, SYSTEM_PROMPT_PATH, CHAT_HISTORY_PATH,
    SIGNAL_PHRASES,
)
from avito_sessions import get_llm_model
from prompts import build_prompt

logger = logging.getLogger(__name__)

# Константы
MAX_HISTORY_MESSAGES: int = 6
MAX_FAQ_MATCHES: int = 5  # Увеличено для лучшего покрытия контекста
FAQ_SIMILARITY_CUTOFF: float = 0.50  # Базовый порог (адаптивный)
FAQ_SIMILARITY_CUTOFF_MIN: float = 0.45  # Минимальный порог для коротких текстов
FAQ_SIMILARITY_CUTOFF_MAX: float = 0.65  # Максимальный порог для длинных текстов

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
if not os.path.exists(DYNAMIC_CONTEXT_PATH):
    with open(DYNAMIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
        f.write("")
if not os.path.exists(SYSTEM_PROMPT_PATH):
    with open(SYSTEM_PROMPT_PATH, "w", encoding="utf-8") as f:
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


def _normalize_text(text: str) -> str:
    """
    Нормализует текст для лучшего сравнения.
    
    Args:
        text: Исходный текст
        
    Returns:
        Нормализованный текст (lowercase, без лишних пробелов)
    """
    if not text:
        return ""
    # Приводим к нижнему регистру и убираем лишние пробелы
    normalized = text.lower().strip()
    # Убираем множественные пробелы
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


def _calculate_adaptive_cutoff(text: str) -> float:
    """
    Вычисляет адаптивный порог схожести в зависимости от длины текста.
    
    Для коротких текстов (1-3 слова) используем более низкий порог,
    для длинных текстов - более высокий.
    
    Args:
        text: Входящий текст
        
    Returns:
        Адаптивный порог схожести (0.45 - 0.65)
    """
    words = len(text.split())
    
    if words <= 3:
        # Короткие вопросы - более низкий порог для улавливания вариаций
        return FAQ_SIMILARITY_CUTOFF_MIN
    elif words <= 10:
        # Средние вопросы - базовый порог
        return FAQ_SIMILARITY_CUTOFF
    else:
        # Длинные вопросы - более высокий порог для точности
        return FAQ_SIMILARITY_CUTOFF_MAX


def _build_faq_context(incoming_text: str, faq_data: List[Dict[str, str]]) -> str:
    """
    Строит контекст из FAQ на основе схожести с входящим текстом.
    
    Использует улучшенный алгоритм поиска:
    - Нормализация текста (lowercase, удаление лишних пробелов)
    - Адаптивный порог схожести в зависимости от длины текста
    - Приоритет совпадений по вопросам над совпадениями по ответам
    - Поиск по ключевым словам в дополнение к SequenceMatcher
    
    Args:
        incoming_text: Входящий текст пользователя
        faq_data: Список FAQ записей [{"question": str, "answer": str}]
        
    Returns:
        Строка с релевантными вопросами и ответами из FAQ
    """
    if not faq_data or not incoming_text:
        return ""
    
    # Нормализуем входящий текст
    normalized_incoming = _normalize_text(incoming_text)
    
    # Вычисляем адаптивный порог
    adaptive_cutoff = _calculate_adaptive_cutoff(normalized_incoming)
    
    # Извлекаем ключевые слова из входящего текста (слова длиннее 3 символов)
    incoming_words = set(word for word in normalized_incoming.split() if len(word) > 3)
    
    # Подготавливаем данные для поиска
    questions = []
    question_to_item = {}
    
    for item in faq_data:
        q = item.get("question", "")
        if q:
            normalized_q = _normalize_text(q)
            questions.append(normalized_q)
            question_to_item[normalized_q] = item
    
    # Находим похожие вопросы с адаптивным порогом
    matched_questions = difflib.get_close_matches(
        normalized_incoming, questions, n=MAX_FAQ_MATCHES * 2, cutoff=adaptive_cutoff
    )
    
    # Дополнительный поиск по ключевым словам
    keyword_matches = []
    for q in questions:
        q_words = set(word for word in q.split() if len(word) > 3)
        # Вычисляем пересечение ключевых слов
        common_words = incoming_words & q_words
        if common_words:
            # Оценка релевантности на основе количества общих слов
            relevance_score = len(common_words) / max(len(incoming_words), len(q_words))
            if relevance_score >= 0.3:  # Порог для ключевых слов
                keyword_matches.append((q, relevance_score))
    
    # Сортируем по релевантности
    keyword_matches.sort(key=lambda x: x[1], reverse=True)
    keyword_questions = [q for q, _ in keyword_matches[:MAX_FAQ_MATCHES]]
    
    # Объединяем результаты (приоритет SequenceMatcher, затем ключевые слова)
    all_matched_questions = []
    seen = set()
    
    # Сначала добавляем совпадения из SequenceMatcher
    for q in matched_questions:
        if q not in seen:
            all_matched_questions.append(q)
            seen.add(q)
    
    # Затем добавляем совпадения по ключевым словам
    for q in keyword_questions:
        if q not in seen and len(all_matched_questions) < MAX_FAQ_MATCHES:
            all_matched_questions.append(q)
            seen.add(q)
    
    # Формируем результат
    parts = []
    for q in all_matched_questions[:MAX_FAQ_MATCHES]:
        item = question_to_item.get(q)
        if item:
            question = item.get("question", "")
            answer = item.get("answer", "")
            if question and answer:
                parts.append(f"Вопрос: {question}\nОтвет: {answer}")
    
    result = "\n\n".join(parts)
    
    if result:
        logger.debug(
            "FAQ context built: incoming_text_length=%d, matches=%d, cutoff=%.2f",
            len(incoming_text), len(parts), adaptive_cutoff
        )
    
    return result


async def generate_reply(
    dialog_id: str,
    incoming_text: str,
    *,
    user_name: Optional[str] = None
) -> Tuple[Optional[str], Dict[str, bool]]:
    """
    Генерирует ответ на основе входящего текста, FAQ и истории диалога.
    
    Единая генерация ответа для Avito и для Telegram.
    
    Args:
        dialog_id: Уникальный ID диалога (например, "avito_123" или "tg_456")
        incoming_text: Входящий текст от пользователя
        user_name: Имя пользователя (опционально)
        
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
    
    # Добавляем новое сообщение пользователя в историю (для сохранения) с временной меткой
    # Но НЕ сохраняем ответ ассистента здесь - это будет сделано в main.py после успешной отправки
    from datetime import datetime
    dialog_history.append({
        "role": "user",
        "content": incoming_text,
        "timestamp": datetime.now().isoformat()
    })
    
    # Сохраняем сообщение пользователя в историю (сохраняем всю историю, без ограничений)
    chat_history[dialog_id] = dialog_history
    _save_json(CHAT_HISTORY_PATH, chat_history)
    
    logger.info(
        "Saved user message to chat history for dialog_id=%s: %d messages",
        dialog_id, len(chat_history[dialog_id])
    )
    
    # Загружаем FAQ и контексты
    faq_data = _load_json(FAQ_PATH, [])
    
    # Загружаем статический контекст
    try:
        with open(STATIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
            static_context = f.read().strip()
    except (FileNotFoundError, IOError) as e:
        logger.warning("Failed to load static context: %s", e)
        static_context = ""
    
    # Загружаем динамический контекст
    try:
        with open(DYNAMIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
            dynamic_context = f.read().strip()
    except (FileNotFoundError, IOError) as e:
        logger.warning("Failed to load dynamic context: %s", e)
        dynamic_context = ""
    
    # Загружаем системный промпт
    try:
        with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
            system_prompt = f.read().strip()
    except (FileNotFoundError, IOError) as e:
        logger.warning("Failed to load system prompt: %s", e)
        system_prompt = ""
    
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
        system_prompt=system_prompt,
        static_context=static_context,
        dynamic_context=dynamic_context,
        dialogue_context=dialogue_context,
        faq_context=faq_context,
        user_name=user_name,
        incoming_text=incoming_text,
    )
    
    # Получаем актуальную модель LLM
    current_model = get_llm_model(LLM_MODEL)
    
    logger.info(
        "Calling LLM for dialog_id=%s, model=%s, prompt_length=%d",
        dialog_id, current_model, len(prompt)
    )
    
    # Проверяем, что клиент инициализирован
    if not client:
        logger.error("OpenAI client not initialized! Cannot generate reply for dialog_id=%s", dialog_id)
        return None, {"contains_signal_phrase": True}
    
    # Генерируем ответ через LLM
    try:
        # Проверяем, поддерживает ли модель temperature
        # Для gpt-5-mini и некоторых других моделей temperature не поддерживается
        use_temperature = current_model not in ["gpt-5-mini", "gpt-5"]
        
        logger.info(
            "Creating chat completion: model=%s, use_temperature=%s, temperature=%s",
            current_model, use_temperature, TEMPERATURE if use_temperature else "N/A"
        )
        
        if use_temperature:
            response = await client.chat.completions.create(
                model=current_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=TEMPERATURE,
            )
        else:
            response = await client.chat.completions.create(
                model=current_model,
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
        
        # Извлекаем информацию о токенах
        usage_info = {}
        if hasattr(response, 'usage') and response.usage:
            usage_info = {
                "prompt_tokens": getattr(response.usage, 'prompt_tokens', 0),
                "completion_tokens": getattr(response.usage, 'completion_tokens', 0),
                "total_tokens": getattr(response.usage, 'total_tokens', 0),
                "model": current_model
            }
            logger.info(
                "Token usage for dialog_id=%s: prompt=%d, completion=%d, total=%d",
                dialog_id, usage_info["prompt_tokens"], usage_info["completion_tokens"], usage_info["total_tokens"]
            )
        
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
    
    # Включаем информацию о токенах в meta
    meta = {"contains_signal_phrase": False}
    if usage_info:
        meta["usage"] = usage_info
    
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
    
    # Обновляем meta с информацией о сигнальной фразе
    meta["contains_signal_phrase"] = contains_signal
    
    return answer, meta
