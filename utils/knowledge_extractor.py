"""
Модуль автоматического извлечения знаний из переписок через LLM.

Анализирует завершенные диалоги и извлекает структурированные знания
в формате knowledge cards.
"""

import logging
import json
import httpx
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from openai import AsyncOpenAI

from config import OPENAI_API_KEY, LLM_MODEL, CHAT_HISTORY_PATH, MIN_DIALOG_AGE_MINUTES
from responder import _load_json, _save_json
from utils.knowledge_cards import upsert_knowledge_cards, CATEGORIES
from utils.chat_history import get_dialog_history, is_dialog_processed, mark_dialog_processed
from avito_sessions import get_llm_model

logger = logging.getLogger(__name__)

# Инициализация OpenAI клиента
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY not set! Knowledge extraction will not work.")
    client = None
else:
    try:
        http_client = httpx.AsyncClient()
        client = AsyncOpenAI(api_key=OPENAI_API_KEY, http_client=http_client)
        logger.info("OpenAI client initialized for knowledge extraction")
    except Exception as e:
        logger.exception("Failed to initialize OpenAI client: %s", e)
        client = None


EXTRACTION_PROMPT = """Ты анализируешь диалог визового консультанта с клиентом. Извлеки структурированные знания в формате JSON.

Извлеки:
1. Темы (topic) - основные вопросы/темы, которые обсуждались
2. Факты (facts) - конкретная информация по каждой теме (цены, сроки, условия, документы и т.д.)
3. Категории - выбери из: визы_шенген, визы_другие, документы, стоимость, сроки, процесс, особые_условия, манера_общения, общее
4. Теги - ключевые слова для поиска
5. Примеры манеры общения (communication_examples) - КРИТИЧЕСКИ ВАЖНО: извлекай примеры того, КАК менеджер общается с клиентами

ОСОБОЕ ВНИМАНИЕ - МАНЕРА ОБЩЕНИЯ:
- Это САМОЕ ВАЖНОЕ для обучения бота! Извлекай примеры человечной, естественной манеры общения менеджера.
- Обращай внимание на:
  * Как менеджер приветствует клиентов
  * Как формулирует ответы (просто, без канцелярита)
  * Какие фразы использует для успокоения клиентов
  * Как предлагает помощь
  * Как объясняет сложные вещи простым языком
  * Тон общения (доброжелательный, спокойный, уверенный)
  * Использование эмодзи (если есть)
  * Структура ответов (коротко, по делу, с конкретикой)
- Создавай отдельные карточки категории "манера_общения" с примерами фраз и стиля общения
- Примеры тем для манеры общения:
  * "Как приветствовать клиентов"
  * "Как объяснять сложные вопросы"
  * "Как успокаивать клиентов"
  * "Как предлагать помощь"
  * "Стиль ответов на вопросы о документах"

КРИТИЧЕСКИ ВАЖНО - ФОРМАТИРОВАНИЕ ТЕМ:
- ВСЕГДА указывай страну в теме, если речь идет о визе в конкретную страну
- Примеры ПРАВИЛЬНЫХ тем:
  * "Требования к документам для визы в Италию"
  * "Стоимость визы в США"
  * "Сроки рассмотрения визы в Грецию"
  * "Особые условия для визы во Францию"
- Примеры НЕПРАВИЛЬНЫХ тем (слишком общих):
  * "Требования к документам" (без страны)
  * "Стоимость визы" (без страны)
  * "Сроки рассмотрения" (без страны)
- Если в диалоге НЕ упоминается конкретная страна, но есть общая информация - используй формат:
  * "Общие требования к документам для шенгенских виз"
  * "Общая стоимость визовых услуг"
- Страны, которые могут упоминаться: Италия, Греция, Франция, Испания, Болгария, Великобритания, США, Япония, Швейцария, Германия, Австрия, Чехия, Польша, Португалия, Нидерланды, Бельгия, Дания, Швеция, Норвегия, Финляндия, Исландия, Мальта, Кипр, Лихтенштейн, Люксембург, Словения, Словакия, Венгрия, Эстония, Латвия, Литва

Формат ответа (строго JSON, без дополнительного текста):
{{
  "cards": [
    {{
      "topic": "Название темы с указанием страны",
      "category": "категория",
      "facts": ["факт 1", "факт 2"],
      "tags": ["тег1", "тег2", "название_страны"],
      "communication_examples": ["пример фразы 1", "пример фразы 2"]  // ТОЛЬКО для категории "манера_общения"
    }}
  ]
}}

ЧТО ИЗВЛЕКАТЬ (стабильная информация):
✅ Общие требования к документам (список документов, требования к оформлению)
✅ Процессы оформления виз (как подавать, куда обращаться)
✅ Общие условия и правила (особенности для разных стран, ограничения)
✅ Стабильные правила работы (условия оплаты, возврата, общие принципы)
✅ Структура услуг (типы виз, категории услуг)
✅ **МАНЕРА ОБЩЕНИЯ МЕНЕДЖЕРА** - это САМОЕ ВАЖНОЕ! Извлекай примеры того, как менеджер общается:
  - Примеры фраз и формулировок
  - Стиль ответов (простой, человечный язык)
  - Тон общения (доброжелательный, спокойный)
  - Как объясняются сложные вещи
  - Как предлагается помощь

ЧТО НЕ ИЗВЛЕКАТЬ (динамическая информация, берется из динамического контекста):
❌ Конкретные даты записи (например, "запись на 15 декабря", "свободные даты на следующую неделю")
❌ Актуальные цены и тарифы (например, "стоимость 50000 рублей", "цена 3000 евро")
❌ Текущие сроки рассмотрения (например, "сейчас рассматривают 45 дней", "на данный момент 2 недели")
❌ Доступность записей (например, "запись доступна на декабрь", "можно записаться на следующую неделю")
❌ Процент одобрения на текущий момент
❌ Любая информация, которая может измениться в ближайшее время

ВАЖНО:
- Извлекай только проверенные факты из диалога
- Не выдумывай информацию
- Группируй связанные факты по темам
- Используй короткие, конкретные формулировки фактов
- ВСЕГДА включай страну в тему, если она упоминается в диалоге
- НЕ извлекай динамическую информацию (даты, цены, сроки) - она хранится в динамическом контексте
- Если информации недостаточно или только динамическая - верни пустой массив cards

Диалог:
{dialogue_text}
"""


async def extract_knowledge_from_dialog(
    dialog_id: str,
    dialogue_history: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Извлекает знания из диалога через LLM.
    
    Args:
        dialog_id: ID диалога
        dialogue_history: История сообщений диалога
        
    Returns:
        Список knowledge cards
    """
    if not client:
        logger.warning("OpenAI client not initialized, cannot extract knowledge")
        return []
    
    if not dialogue_history or len(dialogue_history) < 2:
        return []
    
    # Форматируем диалог для промпта
    dialogue_lines = []
    for msg in dialogue_history:
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        if content:
            role_name = {
                "user": "Клиент",
                "assistant": "Консультант",
                "manager": "Менеджер"
            }.get(role, role.capitalize())
            dialogue_lines.append(f"{role_name}: {content}")
    
    dialogue_text = "\n".join(dialogue_lines)
    
    # Ограничиваем длину диалога
    MAX_DIALOGUE_LENGTH = 8000
    if len(dialogue_text) > MAX_DIALOGUE_LENGTH:
        dialogue_text = dialogue_text[-MAX_DIALOGUE_LENGTH:]
        logger.warning("Dialogue truncated to %d chars for dialog_id=%s", MAX_DIALOGUE_LENGTH, dialog_id)
    
    try:
        current_model = get_llm_model(LLM_MODEL)
        prompt = EXTRACTION_PROMPT.format(dialogue_text=dialogue_text)
        
        response = await client.chat.completions.create(
            model=current_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # Низкая температура для более точного извлечения
        )
        
        if not response.choices or not response.choices[0].message:
            logger.warning("Empty response from LLM for dialog_id=%s", dialog_id)
            return []
        
        answer = response.choices[0].message.content.strip()
        
        # Парсим JSON ответ
        # Убираем markdown code blocks если есть
        if answer.startswith("```"):
            lines = answer.split("\n")
            answer = "\n".join(lines[1:-1]) if len(lines) > 2 else answer
        
        try:
            data = json.loads(answer)
            cards = data.get("cards", [])
            
            # Валидируем и нормализуем карточки
            validated_cards = []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                
                topic = card.get("topic", "").strip()
                if not topic:
                    continue
                
                facts = card.get("facts", [])
                if not isinstance(facts, list):
                    facts = []
                facts = [str(f).strip() for f in facts if f and str(f).strip()]
                
                category = card.get("category", "общее").strip()
                if category not in CATEGORIES:
                    category = "общее"
                
                # Для категории "манера_общения" факты могут быть в communication_examples
                if category == "манера_общения":
                    communication_examples = card.get("communication_examples", [])
                    if not isinstance(communication_examples, list):
                        communication_examples = []
                    communication_examples = [str(e).strip() for e in communication_examples if e and str(e).strip()]
                    # Если есть communication_examples, используем их как facts
                    if communication_examples and not facts:
                        facts = communication_examples
                    elif communication_examples:
                        # Объединяем facts и communication_examples
                        facts = facts + communication_examples
                
                # Для манеры общения facts могут быть пустыми, если есть communication_examples
                if not facts and category != "манера_общения":
                    continue
                
                tags = card.get("tags", [])
                if not isinstance(tags, list):
                    tags = []
                tags = [str(t).strip() for t in tags if t and str(t).strip()]
                
                validated_cards.append({
                    "topic": topic,
                    "category": category,
                    "facts": facts,
                    "tags": tags,
                    "priority": 2  # Средний приоритет для автоматически извлеченных
                })
            
            logger.info("Extracted %d knowledge cards from dialog_id=%s", len(validated_cards), dialog_id)
            return validated_cards
            
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from LLM response for dialog_id=%s: %s", dialog_id, e)
            logger.debug("LLM response: %s", answer[:500])
            return []
            
    except Exception as e:
        logger.exception("Error extracting knowledge from dialog_id=%s: %s", dialog_id, e)
        return []


async def process_dialogs_for_knowledge_extraction(
    *,
    max_dialogs: Optional[int] = None,
    min_age_minutes: int = MIN_DIALOG_AGE_MINUTES
) -> Dict[str, Any]:
    """
    Обрабатывает завершенные диалоги и извлекает знания.
    
    Args:
        max_dialogs: Максимальное количество диалогов для обработки
        min_age_minutes: Минимальный возраст диалога в минутах
        
    Returns:
        Статистика обработки
    """
    if not client:
        logger.warning("OpenAI client not initialized, cannot process dialogs")
        return {"processed": 0, "extracted": 0, "errors": 0}
    
    try:
        all_history = _load_json(CHAT_HISTORY_PATH, {})
    except Exception as e:
        logger.error("Failed to load chat history: %s", e)
        return {"processed": 0, "extracted": 0, "errors": 0}
    
    if not isinstance(all_history, dict):
        return {"processed": 0, "extracted": 0, "errors": 0}
    
    processed = 0
    extracted_total = 0
    errors = 0
    
    now = datetime.now()
    cutoff_time = now - timedelta(minutes=min_age_minutes)
    
    dialog_ids = list(all_history.keys())
    
    # Ограничиваем количество диалогов
    if max_dialogs:
        dialog_ids = dialog_ids[:max_dialogs]
    
    for dialog_id in dialog_ids:
        # Проверяем, не обработан ли уже
        # Используем общий флаг processed (можно улучшить для разных типов обработки)
        if is_dialog_processed(dialog_id):
            continue
        
        try:
            history = all_history.get(dialog_id, [])
            if not isinstance(history, list) or len(history) < 2:
                continue
            
            # Проверяем возраст последнего сообщения
            last_msg = history[-1] if history else None
            if last_msg:
                last_msg_time_str = last_msg.get("timestamp")
                if last_msg_time_str:
                    try:
                        last_msg_time = datetime.fromisoformat(last_msg_time_str.replace("Z", "+00:00"))
                        if last_msg_time > cutoff_time:
                            continue
                    except Exception:
                        pass
            
            # Извлекаем знания
            cards = await extract_knowledge_from_dialog(dialog_id, history)
            
            if cards:
                created, updated = upsert_knowledge_cards(
                    cards,
                    dialog_id=dialog_id,
                    source="llm_extraction"
                )
                extracted_total += created + updated
                logger.info(
                    "Extracted knowledge from dialog_id=%s: created=%d updated=%d",
                    dialog_id, created, updated
                )
            
            # Помечаем как обработанный
            mark_dialog_processed(dialog_id)
            processed += 1
            
        except Exception as e:
            logger.exception("Error processing dialog_id=%s: %s", dialog_id, e)
            errors += 1
    
    logger.info(
        "Knowledge extraction completed: processed=%d, extracted=%d, errors=%d",
        processed, extracted_total, errors
    )
    
    return {
        "processed": processed,
        "extracted": extracted_total,
        "errors": errors
    }

