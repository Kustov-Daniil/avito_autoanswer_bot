"""
Пайплайн для формирования FAQ из истории переписки.

Умный анализ истории диалогов для извлечения вопрос-ответ пар
с учетом контекста всей переписки, а не только последнего сообщения.
"""
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta

from config import CHAT_HISTORY_PATH, MIN_DIALOG_AGE_MINUTES
from responder import _load_json, _save_json
from utils.chat_history import (
    get_dialog_history, is_dialog_processed, mark_dialog_processed
)
from utils.knowledge_cards import upsert_knowledge_cards

logger = logging.getLogger(__name__)


def extract_complete_dialogs(history: List[Dict[str, Any]], min_messages: int = 2) -> List[Dict[str, Any]]:
    """
    Извлекает завершенные диалоги из истории.
    
    Завершенный диалог - это последовательность сообщений, которая заканчивается
    ответом от менеджера или владельца аккаунта.
    
    Args:
        history: История сообщений диалога
        min_messages: Минимальное количество сообщений для диалога
        
    Returns:
        Список завершенных диалогов (каждый диалог - список сообщений)
    """
    if not history or len(history) < min_messages:
        return []
    
    complete_dialogs = []
    current_dialog = []
    
    for msg in history:
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        
        if not content:
            continue
        
        # Если это сообщение от менеджера или владельца - завершаем диалог
        if role in ["manager", "avito_owner"]:
            current_dialog.append(msg)
            if len(current_dialog) >= min_messages:
                complete_dialogs.append(current_dialog.copy())
            current_dialog = []
        else:
            # Добавляем сообщение к текущему диалогу
            current_dialog.append(msg)
    
    return complete_dialogs


def combine_user_messages(dialog: List[Dict[str, Any]]) -> str:
    """
    Объединяет несколько сообщений пользователя в один вопрос.
    
    Клиенты часто отвечают несколькими сообщениями, поэтому нужно
    объединить их в один вопрос для FAQ.
    
    Args:
        dialog: Список сообщений диалога
        
    Returns:
        Объединенный текст вопроса от пользователя
    """
    user_messages = []
    for msg in dialog:
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        
        if role == "user" and content:
            user_messages.append(content)
    
    # Объединяем сообщения пользователя
    if user_messages:
        # Если сообщений несколько, объединяем их через пробел
        # и добавляем контекст из предыдущих сообщений, если они есть
        combined = " ".join(user_messages)
        
        # Если есть сообщения от assistant (предыдущие ответы бота),
        # добавляем их для контекста
        context_parts = []
        for msg in dialog:
            if msg.get("role") == "assistant":
                context_parts.append(msg.get("content", "").strip())
        
        if context_parts:
            # Добавляем контекст в начало вопроса
            context = " ".join(context_parts[-2:])  # Последние 2 ответа для контекста
            combined = f"{context} {combined}"
        
        return combined.strip()
    
    return ""


def extract_answer(dialog: List[Dict[str, Any]]) -> Optional[str]:
    """
    Извлекает ответ из завершенного диалога.
    
    Args:
        dialog: Список сообщений завершенного диалога
        
    Returns:
        Текст ответа или None
    """
    # Ищем последнее сообщение от менеджера или владельца
    for msg in reversed(dialog):
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        
        if role in ["manager", "avito_owner"] and content:
            return content
    
    return None


def format_question_from_dialog(dialog: List[Dict[str, Any]]) -> str:
    """
    Форматирует вопрос из целого диалога, учитывая контекст.
    
    Если последний вопрос короткий (например, "Готовы оформлять?"),
    добавляет контекст из предыдущих сообщений.
    
    Args:
        dialog: Список сообщений диалога
        
    Returns:
        Отформатированный вопрос с контекстом
    """
    # Получаем все сообщения пользователя
    user_messages = [msg.get("content", "").strip() for msg in dialog if msg.get("role") == "user"]
    
    if not user_messages:
        return ""
    
    # Последнее сообщение пользователя
    last_message = user_messages[-1]
    
    # Если последнее сообщение короткое (менее 20 символов),
    # добавляем контекст из предыдущих сообщений
    if len(last_message) < 20 and len(user_messages) > 1:
        # Берем предыдущие сообщения для контекста
        context_messages = user_messages[:-1]
        context = " ".join(context_messages[-2:])  # Последние 2 сообщения для контекста
        return f"{context} {last_message}".strip()
    
    # Если сообщений несколько, объединяем их
    if len(user_messages) > 1:
        return " ".join(user_messages)
    
    return last_message


async def generate_faq_from_dialog(dialog: List[Dict[str, Any]], llm_client) -> Optional[Dict[str, str]]:
    """
    Генерирует FAQ запись из диалога с помощью LLM.
    
    Использует LLM для извлечения вопроса и ответа из целой истории переписки,
    учитывая контекст всех сообщений.
    
    Args:
        dialog: Список сообщений диалога
        llm_client: Клиент OpenAI для генерации
        
    Returns:
        Словарь с "question" и "answer" или None
    """
    if not llm_client:
        logger.warning("LLM client not available for FAQ generation")
        return None
    
    # Форматируем диалог для промпта
    dialog_text = ""
    for msg in dialog:
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        timestamp = msg.get("timestamp", "")
        
        if role == "user":
            dialog_text += f"👤 Клиент: {content}\n"
        elif role == "assistant":
            dialog_text += f"🤖 Бот: {content}\n"
        elif role in ["manager", "avito_owner"]:
            dialog_text += f"👨‍💼 Менеджер: {content}\n"
    
    prompt = f"""Проанализируй следующую переписку и извлеки из неё вопрос клиента и правильный ответ менеджера.

ВАЖНО:
1. Вопрос должен быть полным и понятным, даже если клиент задал его несколькими сообщениями
2. Если последний вопрос короткий (например, "Готовы оформлять?"), включи контекст из предыдущих сообщений
3. Ответ должен быть полным и информативным
4. Убери из ответа личные обращения и временные детали

Переписка:
{dialog_text}

Верни ответ в формате JSON:
{{
  "question": "полный вопрос клиента с контекстом",
  "answer": "полный ответ менеджера"
}}

Только JSON, без дополнительных комментариев."""

    try:
        from avito_sessions import get_llm_model
        from config import LLM_MODEL
        
        model = get_llm_model(LLM_MODEL)
        
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        
        if not response.choices or not response.choices[0].message:
            return None
        
        result_text = response.choices[0].message.content.strip()
        
        # Парсим JSON ответ
        import json
        # Убираем markdown код блоки, если есть
        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
        
        result = json.loads(result_text)
        
        question = result.get("question", "").strip()
        answer = result.get("answer", "").strip()
        
        if question and answer:
            return {"question": question, "answer": answer}
        
        return None
        
    except Exception as e:
        logger.exception("Ошибка при генерации FAQ из диалога: %s", e)
        return None


async def generate_faq_entries_from_history(
    history: List[Dict[str, Any]],
    llm_client,
    *,
    max_messages: int = 60
) -> List[Dict[str, str]]:
    """
    Генерирует 1..N FAQ/knowledge записей из ПОЛНОЙ истории диалога.

    Ключевая идея: не дробить на мелкие Q/A, а группировать follow-up вопросы
    (цена/сроки/география/условия) в одну запись вокруг одной темы.

    Args:
        history: Полная история сообщений диалога
        llm_client: Клиент OpenAI для генерации
        max_messages: Сколько последних сообщений брать в анализ (защита от очень длинных диалогов)

    Returns:
        Список словарей вида {"question": str, "answer": str}
    """
    if not llm_client:
        return []

    if not history:
        return []

    trimmed = history[-max_messages:] if len(history) > max_messages else history

    dialog_text = ""
    for msg in trimmed:
        role = msg.get("role", "")
        content = (msg.get("content", "") or "").strip()
        if not content:
            continue
        if role == "user":
            dialog_text += f"👤 Клиент: {content}\n"
        elif role == "assistant":
            dialog_text += f"🤖 Бот: {content}\n"
        elif role in ["manager", "avito_owner"]:
            dialog_text += f"👨‍💼 Менеджер: {content}\n"

    prompt = f"""Ты — помощник, который формирует базу знаний из переписки с клиентом.

ЗАДАЧА:
Сформируй *минимальное* количество полезных FAQ/knowledge записей из переписки ниже.

КРИТИЧЕСКИ ВАЖНО (про качество):
- Если клиент задаёт уточняющие вопросы в рамках одной темы (например: "делаете визу?", затем "сколько стоит?", затем "где можно податься?") — НЕ делай 3 отдельных записи.
  Вместо этого сделай ОДНУ запись, где:
  - question: обобщённый вопрос по теме (например: "Оформляете ли вы визу в США и какие условия/стоимость/где подача?")
  - answer: структурированный ответ (лучше списком), который включает ВСЕ существенные факты из переписки: доступность/стоимость/ограничения/география и т.д.
- Объединяй ответы менеджера, если он отвечает несколькими сообщениями (например: "нет" + "только Казахстан или Варшава") — это один ответ.
- Не выдумывай факты: используй только то, что явно есть в переписке.
- Игнорируй эмоции/вежливости/реакции вроде "Жаль".

ФОРМАТ:
Верни строго JSON-массив объектов:
[
  {{"question": "...", "answer": "..."}},
  ...
]

Переписка:
{dialog_text}
"""

    try:
        from avito_sessions import get_llm_model
        from config import LLM_MODEL

        model = get_llm_model(LLM_MODEL)
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )

        if not response.choices or not response.choices[0].message:
            return []

        result_text = (response.choices[0].message.content or "").strip()

        import json
        if result_text.startswith("```"):
            parts = result_text.split("```")
            if len(parts) >= 2:
                result_text = parts[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
            result_text = result_text.strip()

        parsed = json.loads(result_text)
        if not isinstance(parsed, list):
            return []

        out: List[Dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            q = (item.get("question") or "").strip()
            a = (item.get("answer") or "").strip()
            if q and a:
                out.append({"question": q, "answer": a})
        return out
    except Exception as e:
        logger.exception("Ошибка при генерации FAQ из полной истории: %s", e)
        return []


async def generate_knowledge_cards_from_history(
    history: List[Dict[str, Any]],
    llm_client,
    *,
    max_messages: int = 80
) -> List[Dict[str, Any]]:
    """
    Генерирует knowledge cards из полной истории диалога.

    Формат карточки:
    {
      "topic": str,
      "facts": [str, ...],
      "tags": [str, ...] (опционально)
    }
    """
    if not llm_client or not history:
        return []

    trimmed = history[-max_messages:] if len(history) > max_messages else history

    dialog_text = ""
    for msg in trimmed:
        role = msg.get("role", "")
        content = (msg.get("content", "") or "").strip()
        if not content:
            continue
        if role == "user":
            dialog_text += f"👤 Клиент: {content}\n"
        elif role == "assistant":
            dialog_text += f"🤖 Бот: {content}\n"
        elif role in ["manager", "avito_owner"]:
            dialog_text += f"👨‍💼 Менеджер: {content}\n"

    prompt = f"""Ты — помощник, который формирует базу знаний из переписки с клиентом.

Сформируй *минимальное* количество knowledge cards из переписки.

Правила:
- Follow-up вопросы в рамках одной темы (стоимость/сроки/где подача/ограничения) должны попасть в ОДНУ карточку.
- Объединяй ответы менеджера, даже если он отвечает несколькими сообщениями.
- Не выдумывай факты. Только то, что есть в переписке.
- Пиши факты коротко и конкретно (лучше буллетами).
- Игнорируй эмоции/вежливости/реакции (например, "Жаль").

Верни строго JSON-массив:
[
  {{"topic": "...", "facts": ["...", "..."], "tags": ["..."]}},
  ...
]

Переписка:
{dialog_text}
"""

    try:
        from avito_sessions import get_llm_model
        from config import LLM_MODEL

        model = get_llm_model(LLM_MODEL)
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )

        if not response.choices or not response.choices[0].message:
            return []

        result_text = (response.choices[0].message.content or "").strip()

        import json
        if result_text.startswith("```"):
            parts = result_text.split("```")
            if len(parts) >= 2:
                result_text = parts[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
            result_text = result_text.strip()

        parsed = json.loads(result_text)
        if not isinstance(parsed, list):
            return []

        out: List[Dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            topic = (item.get("topic") or "").strip()
            facts = item.get("facts") or []
            tags = item.get("tags") or []
            if not topic:
                continue
            if not isinstance(facts, list):
                facts = []
            facts = [str(x).strip() for x in facts if str(x).strip()]
            if not facts:
                continue
            if not isinstance(tags, list):
                tags = []
            tags = [str(x).strip() for x in tags if str(x).strip()]
            out.append({"topic": topic, "facts": facts, "tags": tags})
        return out
    except Exception as e:
        logger.exception("Ошибка при генерации knowledge cards из истории: %s", e)
        return []


async def process_dialog_for_faq(dialog_id: str, llm_client=None) -> int:
    """
    Обрабатывает диалог для извлечения FAQ записей.
    
    Args:
        dialog_id: ID диалога (например, "avito_123")
        llm_client: Клиент OpenAI (опционально, для LLM-генерации)
        
    Returns:
        Количество добавленных FAQ записей
    """
    try:
        # Проверяем, не был ли диалог уже обработан
        if is_dialog_processed(dialog_id):
            logger.debug("Dialog already processed, skipping: dialog_id=%s", dialog_id)
            return 0
        
        history = get_dialog_history(dialog_id)
        
        if not history or len(history) < 2:
            return 0
        
        # Извлекаем завершенные диалоги
        complete_dialogs = extract_complete_dialogs(history, min_messages=2)
        
        if not complete_dialogs:
            return 0
        
        added_count = 0  # количество добавленных/обновлённых knowledge cards (для совместимости со статистикой)
        cards_changed = False

        # LLM-ветка: строим knowledge cards (основной формат знаний)
        if llm_client:
            cards = await generate_knowledge_cards_from_history(history, llm_client)
            if cards:
                created, updated = upsert_knowledge_cards(cards, dialog_id=dialog_id, source="history_learning")
                cards_changed = (created + updated) > 0
                added_count += (created + updated)
            else:
                # fallback: grouped Q/A из истории (если cards не получилось получить) → конвертируем в cards
                entries = await generate_faq_entries_from_history(history, llm_client)
                fallback_cards: List[Dict[str, Any]] = []
                for entry in entries:
                    question = (entry.get("question") or "").strip()
                    answer = (entry.get("answer") or "").strip()
                    if not question or not answer:
                        continue
                    facts = [line.strip("-• \t").strip() for line in answer.splitlines() if line.strip()]
                    if not facts:
                        facts = [answer]
                    fallback_cards.append({"topic": question, "facts": facts, "tags": []})
                if fallback_cards:
                    created, updated = upsert_knowledge_cards(fallback_cards, dialog_id=dialog_id, source="history_learning")
                    cards_changed = (created + updated) > 0
                    added_count += (created + updated)
        else:
            # Fallback без LLM: обрабатываем каждый завершенный диалог (простая эвристика)
            for dialog in complete_dialogs:
                answer = extract_answer(dialog)
                if not answer:
                    continue
                question = format_question_from_dialog(dialog) or combine_user_messages(dialog)
                if not question or not answer:
                    continue
                created, updated = upsert_knowledge_cards(
                    [{"topic": question, "facts": [answer], "tags": []}],
                    dialog_id=dialog_id,
                    source="history_learning",
                )
                if (created + updated) > 0:
                    added_count += (created + updated)
                    cards_changed = True
        
        # Отмечаем диалог как обработанный, если были добавлены/обновлены карточки
        if added_count > 0 or cards_changed:
            mark_dialog_processed(dialog_id)
            logger.debug("Marked dialog as processed: dialog_id=%s, cards_changed_count=%d", dialog_id, added_count)
        
        return added_count
        
    except Exception as e:
        logger.exception("Ошибка при обработке диалога для FAQ: %s", e)
        return 0


async def process_all_dialogs_for_faq(llm_client=None, min_dialog_age_minutes: Optional[int] = None) -> Dict[str, int]:
    """
    Обрабатывает все диалоги для извлечения FAQ записей.
    
    Обрабатывает только диалоги, которые не обновлялись в течение указанного времени
    (чтобы не обрабатывать активные диалоги).
    
    Args:
        llm_client: Клиент OpenAI (опционально)
        min_dialog_age_minutes: Минимальный возраст диалога в минутах для обработки
                                (если None, используется значение из конфига)
        
    Returns:
        Словарь с статистикой: {"processed": int, "added": int}
    """
    # Используем значение из конфига, если не указано явно
    if min_dialog_age_minutes is None:
        min_dialog_age_minutes = MIN_DIALOG_AGE_MINUTES
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        
        if not chat_history:
            return {"processed": 0, "added": 0}
        
        now = datetime.now()
        processed_count = 0
        total_added = 0
        
        for dialog_id, history in chat_history.items():
            # Пропускаем метаданные
            if dialog_id == "_meta":
                continue
            
            if not history or not isinstance(history, list):
                continue
            
            # Проверяем, не был ли диалог уже обработан
            if is_dialog_processed(dialog_id):
                logger.debug("Skipping already processed dialog: dialog_id=%s", dialog_id)
                continue
            
            # Проверяем возраст последнего сообщения (только если min_dialog_age_minutes > 0)
            if min_dialog_age_minutes > 0:
                last_msg = history[-1] if history else None
                if last_msg:
                    timestamp_str = last_msg.get("timestamp")
                    if timestamp_str:
                        try:
                            last_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                            # Убираем timezone для сравнения
                            if last_time.tzinfo:
                                last_time = last_time.replace(tzinfo=None)
                            
                            age_minutes = (now - last_time).total_seconds() / 60
                            if age_minutes < min_dialog_age_minutes:
                                continue  # Пропускаем свежие диалоги
                        except Exception:
                            pass  # Если не удалось распарсить время, обрабатываем
            
            # Обрабатываем диалог
            was_processed = is_dialog_processed(dialog_id)
            added = await process_dialog_for_faq(dialog_id, llm_client)
            now_processed = is_dialog_processed(dialog_id)
            if (not was_processed) and now_processed:
                processed_count += 1
            if added > 0:
                total_added += added
        
        logger.info(
            "Обработка всех диалогов завершена: processed=%d, added=%d",
            processed_count, total_added
        )
        
        return {"processed": processed_count, "added": total_added}
        
    except Exception as e:
        logger.exception("Ошибка при обработке всех диалогов: %s", e)
        return {"processed": 0, "added": 0}

