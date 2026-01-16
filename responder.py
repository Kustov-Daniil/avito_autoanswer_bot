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
    DATA_DIR, FAQ_PATH, KNOWLEDGE_CARDS_PATH, STATIC_CONTEXT_PATH, DYNAMIC_CONTEXT_PATH, SYSTEM_PROMPT_PATH, CHAT_HISTORY_PATH,
    SIGNAL_PHRASES,
)
from avito_sessions import get_llm_model
from prompts import build_prompt

logger = logging.getLogger(__name__)

# Константы
MAX_HISTORY_MESSAGES: int = 8
MAX_FAQ_MATCHES: int = 5  # Кол-во Q/A из curated FAQ, которые можно подмешать в промпт
FAQ_SIMILARITY_CUTOFF: float = 0.50  # Базовый порог (адаптивный)
FAQ_SIMILARITY_CUTOFF_MIN: float = 0.45  # Минимальный порог для коротких текстов
FAQ_SIMILARITY_CUTOFF_MAX: float = 0.65  # Максимальный порог для длинных текстов
FAQ_EXACT_MATCH_THRESHOLD: float = 0.93  # Порог для точного совпадения (curated FAQ → можно отвечать без LLM)
MAX_AVITO_MESSAGE_LENGTH: int = 950  # Ограничение Avito API

# В prompt нельзя бесконтрольно вливать файлы: ограничиваем объём контекста по символам
MAX_STATIC_CONTEXT_CHARS: int = 8000
MAX_DYNAMIC_CONTEXT_CHARS: int = 8000
MAX_DIALOGUE_CONTEXT_CHARS: int = 3000

# FAQ как отдельный источник оставляем только “curated” (ручные/менеджерские), чтобы не засорять промпт
FAQ_ALLOWED_SOURCES: set[str] = {"admin", "manager"}

# Инициализация OpenAI клиента
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY not set! LLM functionality will not work.")
    http_client = None
    client = None
else:
    try:
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        # max_retries поддерживается в openai>=1.x; если в окружении иначе — просто игнорируем параметр через try/except выше.
        client = AsyncOpenAI(api_key=OPENAI_API_KEY, http_client=http_client, max_retries=3)
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
if not os.path.exists(KNOWLEDGE_CARDS_PATH):
    with open(KNOWLEDGE_CARDS_PATH, "w", encoding="utf-8") as f:
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
    
    Убирает ссылки, @упоминания, пунктуацию, приводит к нижнему регистру.
    
    Args:
        text: Исходный текст
        
    Returns:
        Нормализованный текст (lowercase, без ссылок, упоминаний, пунктуации)
    """
    if not text:
        return ""
    # Приводим к нижнему регистру
    normalized = text.lower().strip()
    # Убираем ссылки (http://, https://, www.)
    normalized = re.sub(r'https?://\S+|www\.\S+', '', normalized)
    # Убираем @упоминания
    normalized = re.sub(r'@\w+', '', normalized)
    # Убираем пунктуацию
    normalized = re.sub(r'[^\w\s]', '', normalized)
    # Убираем множественные пробелы
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip()


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


def _truncate_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars - 120:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "..."


def _sanitize_answer(text: str) -> str:
    """
    Легкая санитизация для Avito/Telegram:
    - убираем markdown-маркеры, которые часто “ломают” восприятие
    """
    if not text:
        return ""
    out = text.replace("*", "").replace("#", "")
    return out.strip()


def _find_exact_faq_match(incoming_text: str, faq_data: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """
    Ищет точное совпадение вопроса с FAQ (≥0.9 схожести после нормализации).
    
    Используется для curated FAQ: если матч очень высокий, можно отвечать
    напрямую из базы (без LLM), чтобы снизить стоимость и риск галлюцинаций.
    
    Args:
        incoming_text: Входящий текст пользователя
        faq_data: Список FAQ записей [{"question": str, "answer": str}]
        
    Returns:
        FAQ запись с максимальной схожестью ≥0.9 или None если не найдено
    """
    if not faq_data or not incoming_text:
        return None
    
    # Нормализуем входящий текст
    normalized_incoming = _normalize_text(incoming_text)
    
    best_match = None
    best_score = 0.0
    
    for item in faq_data:
        q = item.get("question", "")
        if not q:
            continue
        
        normalized_q = _normalize_text(q)
        
        # Вычисляем схожесть
        similarity = difflib.SequenceMatcher(None, normalized_incoming, normalized_q).ratio()
        
        if similarity > best_score:
            best_score = similarity
            best_match = item
    
    # Возвращаем только если схожесть ≥0.9
    if best_score >= FAQ_EXACT_MATCH_THRESHOLD:
        logger.info(
            "Exact FAQ match found: similarity=%.2f, question='%s'",
            best_score, best_match.get("question", "")[:50] if best_match else ""
        )
        return best_match
    
    return None


def _build_faq_context(incoming_text: str, faq_data: List[Dict[str, str]]) -> str:
    """
    Строит контекст из curated FAQ (ручные/менеджерские записи) на основе схожести.
    
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
    
    # Формируем результат в структурированном формате
    parts = []
    for q in all_matched_questions[:MAX_FAQ_MATCHES]:
        item = question_to_item.get(q)
        if item:
            question = item.get("question", "")
            answer = item.get("answer", "")
            if question and answer:
                parts.append(f"**Вопрос:** {question}\n**Ответ:** {answer}")
    
    result = "\n\n".join(parts)
    
    if result:
        logger.debug(
            "FAQ context built: incoming_text_length=%d, matches=%d, cutoff=%.2f",
            len(incoming_text), len(parts), adaptive_cutoff
        )
    
    return result


def _build_knowledge_context(incoming_text: str, cards: List[Dict[str, Any]]) -> str:
    """
    Строит контекст из knowledge cards (темы + факты), релевантных входящему вопросу.
    
    Использует улучшенный семантический поиск с ранжированием.
    """
    if not cards or not incoming_text:
        return ""

    # Используем улучшенный поиск из knowledge_cards
    try:
        from utils.knowledge_cards import search_knowledge_cards, update_usage, load_knowledge_cards
        
        # Ищем релевантные карточки (исключая манеру общения - она добавляется отдельно)
        scored_results = search_knowledge_cards(
            incoming_text,
            limit=5,  # Берем топ-5 для лучшего контекста
            min_relevance=0.3
        )
        
        # ВСЕГДА добавляем примеры манеры общения (независимо от запроса)
        # Это критически важно для обучения бота правильному стилю общения
        all_cards = load_knowledge_cards()
        communication_cards_all = [
            c for c in all_cards 
            if c.get("category") == "манера_общения" and c.get("facts")
        ]
        
        # Берем разнообразные примеры манеры общения (до 5 карточек)
        # Приоритет: сначала те, которые имеют высокий usage_count или priority
        communication_cards_sorted = sorted(
            communication_cards_all,
            key=lambda c: (
                c.get("priority", 2),  # Сначала высокий приоритет
                -c.get("usage_count", 0),  # Потом часто используемые
                -c.get("relevance_score", 0.5)  # Потом высокая релевантность
            )
        )
        
        # Добавляем примеры манеры общения к результатам (всегда, если они есть)
        communication_cards_for_context = communication_cards_sorted[:5]
        
        # Форматируем результат в структурированном виде
        lines: List[str] = []
        
        # Сначала обычные карточки (топ-3)
        if scored_results:
            # Обновляем статистику использования для найденных карточек
            for _, card in scored_results[:3]:
                try:
                    update_usage(card)
                except Exception as e:
                    logger.warning("Failed to update usage for card: %s", e)
            
            # Фильтруем только обычные карточки (не манеру общения)
            regular_cards = [
                (score, c) for score, c in scored_results
                if c.get("category") != "манера_общения"
            ]
            
            for score, c in regular_cards[:3]:
                topic = (c.get("topic") or "").strip()
                facts = c.get("facts") or []
                if topic:
                    lines.append(f"**{topic}**")
                if isinstance(facts, list) and facts:
                    for f in facts[:8]:  # Ограничиваем количество фактов для читаемости
                        f_txt = str(f).strip()
                        if f_txt:
                            lines.append(f"- {f_txt}")
                lines.append("")  # Пустая строка между темами для читаемости
        
        # ВСЕГДА добавляем примеры манеры общения (критически важно для стиля)
        if communication_cards_for_context:
            lines.append("**🎯 ПРИМЕРЫ МАНЕРЫ ОБЩЕНИЯ МЕНЕДЖЕРА (используй этот стиль):**")
            lines.append("")
            
            # Собираем все примеры из карточек
            all_examples = []
            for c in communication_cards_for_context:
                facts = c.get("facts") or []
                if isinstance(facts, list) and facts:
                    # Берем по 2-3 примера из каждой карточки
                    examples_from_card = [str(f).strip() for f in facts[:3] if f and str(f).strip()]
                    all_examples.extend(examples_from_card)
            
            # Ограничиваем общее количество примеров (чтобы не перегружать контекст)
            for example in all_examples[:10]:  # Максимум 10 примеров
                lines.append(f'💬 "{example}"')
            
            lines.append("")
            lines.append("ВАЖНО: Отвечай в том же стиле, что и в примерах выше - просто, человечно, без канцелярита.")
            lines.append("")
        
        if lines:
            return "\n".join(lines).strip()
        
        return ""
            
    except ImportError:
        logger.warning("Failed to import search_knowledge_cards, using fallback method")
        # Fallback на старый метод если новый модуль недоступен
        return _build_knowledge_context_fallback(incoming_text, cards)
    except Exception as e:
        logger.exception("Error in improved knowledge search, using fallback: %s", e)
        return _build_knowledge_context_fallback(incoming_text, cards)


def _build_knowledge_context_fallback(incoming_text: str, cards: List[Dict[str, Any]]) -> str:
    """Fallback метод для построения контекста (старая логика)."""
    if not cards:
        return ""

    # Разделяем карточки на обычные и манеру общения
    regular_cards = []
    communication_cards = []
    
    for c in cards:
        category = c.get("category", "")
        if category == "манера_общения":
            communication_cards.append(c)
        else:
            regular_cards.append(c)
    
    # Обрабатываем обычные карточки
    normalized_incoming = _normalize_text(incoming_text) if incoming_text else ""
    incoming_words = set(word for word in normalized_incoming.split() if len(word) > 3) if normalized_incoming else set()

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for c in regular_cards:
        topic = c.get("topic", "") or ""
        facts = c.get("facts") or []
        if not topic and not facts:
            continue

        topic_n = _normalize_text(str(topic))
        facts_text = " ".join([str(x) for x in facts]) if isinstance(facts, list) else str(facts)
        facts_n = _normalize_text(facts_text)

        if normalized_incoming:
            s_topic = difflib.SequenceMatcher(None, normalized_incoming, topic_n).ratio() if topic_n else 0.0
            s_facts = difflib.SequenceMatcher(None, normalized_incoming, facts_n).ratio() if facts_n else 0.0
            score = max(s_topic, s_facts)

            card_words = set(word for word in (topic_n + " " + facts_n).split() if len(word) > 3)
            common = incoming_words & card_words
            if common:
                score = min(1.0, score + 0.10 * min(3, len(common)))
        else:
            score = 0.5  # Если нет входящего текста, даем средний score

        if score >= 0.35:
            scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_regular = [c for _, c in scored[:3]]

    lines: List[str] = []
    
    # Обычные карточки
    for c in top_regular:
        topic = (c.get("topic") or "").strip()
        facts = c.get("facts") or []
        if topic:
            lines.append(f"**{topic}**")
        if isinstance(facts, list) and facts:
            for f in facts[:10]:
                f_txt = str(f).strip()
                if f_txt:
                    lines.append(f"- {f_txt}")
        lines.append("")
    
    # ВСЕГДА добавляем примеры манеры общения
    if communication_cards:
        # Сортируем по приоритету и использованию
        communication_cards_sorted = sorted(
            communication_cards,
            key=lambda c: (
                c.get("priority", 2),
                -c.get("usage_count", 0),
                -c.get("relevance_score", 0.5)
            )
        )
        
        lines.append("**🎯 ПРИМЕРЫ МАНЕРЫ ОБЩЕНИЯ МЕНЕДЖЕРА (используй этот стиль):**")
        lines.append("")
        
        all_examples = []
        for c in communication_cards_sorted[:5]:
            facts = c.get("facts") or []
            if isinstance(facts, list) and facts:
                examples_from_card = [str(f).strip() for f in facts[:3] if f and str(f).strip()]
                all_examples.extend(examples_from_card)
        
        for example in all_examples[:10]:
            lines.append(f'💬 "{example}"')
        
        lines.append("")
        lines.append("ВАЖНО: Отвечай в том же стиле, что и в примерах выше - просто, человечно, без канцелярита.")
        lines.append("")
    
    return "\n".join(lines).strip() if lines else ""


async def generate_reply(
    dialog_id: str,
    incoming_text: str,
    *,
    user_name: Optional[str] = None,
    save_user_message_to_history: bool = True,
) -> Tuple[Optional[str], Dict[str, bool]]:
    """
    Генерирует ответ на основе входящего текста и базы знаний.
    
    Единая генерация ответа для Avito и для Telegram.
    
    Логика:
    - (опционально) Curated FAQ: при очень высоком матче (>= threshold) отвечаем напрямую
      (дешевле и надежнее).
    - Иначе: LLM отвечает, опираясь на knowledge cards + dynamic/static + диалог.
    - Если данных недостаточно: LLM должен вернуть сигнальную фразу → эскалация менеджеру.
    
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
    
    # Загружаем curated FAQ (ручные/менеджерские)
    faq_all = _load_json(FAQ_PATH, [])
    faq_data: List[Dict[str, str]] = []
    if isinstance(faq_all, list):
        for item in faq_all:
            if not isinstance(item, dict):
                continue
            src = (item.get("source") or "").strip().lower()
            if src in FAQ_ALLOWED_SOURCES:
                faq_data.append(item)

    # Загружаем историю диалога (контекст берем ДО сохранения нового сообщения)
    chat_history = _load_json(CHAT_HISTORY_PATH, {})
    dialog_history = chat_history.get(dialog_id, [])

    dialog_history_for_context = dialog_history[-MAX_HISTORY_MESSAGES:] if dialog_history else []

    # Сохраняем сообщение пользователя в историю (опционально; Avito webhook уже сохраняет сам)
    if save_user_message_to_history:
        from utils.chat_history import save_user_message
        save_user_message(dialog_id, incoming_text)
        logger.info("Saved user message to chat history for dialog_id=%s", dialog_id)

    # Curated FAQ: если нашли прямой матч — отвечаем напрямую (без LLM)
    exact_match = _find_exact_faq_match(incoming_text, faq_data)
    if exact_match:
        faq_answer = _sanitize_answer((exact_match.get("answer") or "").strip())
        faq_answer = _truncate_text(faq_answer, MAX_AVITO_MESSAGE_LENGTH)
        if faq_answer:
            return faq_answer, {"contains_signal_phrase": False}

    logger.info("No exact curated FAQ match, generating answer via LLM for dialog_id=%s", dialog_id)
    
    logger.info("Loaded chat history for dialog_id=%s: %d messages", dialog_id, len(dialog_history))
    
    # Загружаем системный промпт (манера поведения и стиль общения)
    # Содержит только инструкции о том, КАК отвечать, не содержит фактов о компании
    try:
        with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
            system_prompt = f.read().strip()
    except (FileNotFoundError, IOError) as e:
        logger.warning("Failed to load system prompt: %s", e)
        system_prompt = ""
    
    # Загружаем статический контекст (миссия компании, описание услуг)
    # Содержит информацию, которая не меняется часто: миссия, общее описание услуг
    try:
        with open(STATIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
            static_context = _truncate_text(f.read().strip(), MAX_STATIC_CONTEXT_CHARS)
    except (FileNotFoundError, IOError) as e:
        logger.warning("Failed to load static context: %s", e)
        static_context = ""
    
    # Загружаем динамический контекст (актуальные цены, тарифы, сроки)
    # Содержит информацию, которая меняется регулярно: цены, сроки подачи, доступность записей
    # Имеет приоритет над другими источниками для цен и сроков
    try:
        with open(DYNAMIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
            dynamic_context = _truncate_text(f.read().strip(), MAX_DYNAMIC_CONTEXT_CHARS)
    except (FileNotFoundError, IOError) as e:
        logger.warning("Failed to load dynamic context: %s", e)
        dynamic_context = ""
    
    # Строим контексты - используем историю БЕЗ нового сообщения пользователя для контекста
    # (новое сообщение передается отдельно в incoming_text)
    # Примечание: FAQ больше не используется в промпте, только для exact match выше
    
    # Загружаем knowledge cards и строим контекст с улучшенным поиском
    try:
        from utils.knowledge_cards import load_knowledge_cards
        knowledge_cards = load_knowledge_cards()
    except ImportError:
        # Fallback на старый метод
        knowledge_cards = _load_json(KNOWLEDGE_CARDS_PATH, [])
    
    knowledge_context = _build_knowledge_context(
        incoming_text,
        knowledge_cards if isinstance(knowledge_cards, list) else []
    )
    # Форматируем контекст диалога в читаемом виде
    dialogue_lines = []
    for m in dialog_history_for_context:
        role = m.get("role", "").capitalize()
        content = m.get("content", "")
        if role and content:
            dialogue_lines.append(f"{role}: {content}")
    dialogue_context = "\n".join(dialogue_lines)
    dialogue_context = _truncate_text(dialogue_context, MAX_DIALOGUE_CONTEXT_CHARS)
    
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
        knowledge_context=knowledge_context,
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
            answer = _sanitize_answer(answer.strip())
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
    looks_like_uncertainty = any(
        x in answer_lower
        for x in [
            "не знаю",
            "не могу ответить",
            "нет информации",
            "не располагаю",
            "недостаточно данных",
        ]
    )
    if looks_like_uncertainty:
        contains_signal = True
    
    # Если обнаружена сигнальная фраза - заменяем весь ответ на сообщение для клиента
    # Клиент не должен знать, что его передают менеджеру
    if contains_signal:
        logger.info("Generated reply contains signal phrase for dialog_id=%s, replacing with client message", dialog_id)
        # Заменяем весь ответ на сообщение для клиента
        answer = "Подождите, пожалуйста, уточняю информацию"
    else:
        answer = _truncate_text(answer, MAX_AVITO_MESSAGE_LENGTH)
    
    # НЕ сохраняем ответ в историю здесь - это будет сделано в main.py после успешной отправки
    # Это нужно, чтобы не сохранять ответы, которые не были отправлены клиенту
    
    # Обновляем meta с информацией о сигнальной фразе
    meta["contains_signal_phrase"] = contains_signal
    
    return answer, meta
