"""
Knowledge cards — улучшенная структурированная база знаний.

Улучшенный формат:
- Категории для организации знаний
- Приоритет и релевантность для поиска
- Автоматическое извлечение из переписок через LLM
- Ручное добавление админом
- Семантический поиск с ранжированием

Формат карточки:
{
  "topic": "Виза в Италию",
  "category": "визы_шенген",  // Категория для группировки
  "facts": [
    "Срок оформления: 60 дней",
    "Стоимость: зависит от типа визы"
  ],
  "tags": ["италия", "шенген", "виза"],
  "priority": 1,  // 1-высокий, 2-средний, 3-низкий
  "relevance_score": 0.95,  // Оценка релевантности (0-1)
  "source": "history_learning" | "admin_manual" | "llm_extraction",
  "dialog_ids": ["avito_u2u-..."],
  "created_at": "2025-12-12T13:06:35.939878",
  "updated_at": "2025-12-12T13:06:35.939878",
  "usage_count": 5,  // Сколько раз использовалась
  "last_used_at": "2025-12-12T15:02:44.047139"
}
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import difflib

from config import KNOWLEDGE_CARDS_PATH
from responder import _load_json, _save_json

logger = logging.getLogger(__name__)

# Категории знаний
CATEGORIES = {
    "визы_шенген": "Визы в страны Шенгенской зоны",
    "визы_другие": "Визы в другие страны",
    "документы": "Документы и требования",
    "стоимость": "Стоимость и оплата",
    "сроки": "Сроки оформления и рассмотрения",
    "процесс": "Процесс оформления",
    "особые_условия": "Особые условия и ограничения",
    "манера_общения": "Примеры манеры общения менеджера",
    "общее": "Общая информация"
}

# Приоритеты
PRIORITY_HIGH = 1
PRIORITY_MEDIUM = 2
PRIORITY_LOW = 3


def _norm(s: str) -> str:
    """Нормализует строку для сравнения."""
    return (s or "").strip().lower()


def load_knowledge_cards() -> List[Dict[str, Any]]:
    """Загружает все knowledge cards."""
    data = _load_json(KNOWLEDGE_CARDS_PATH, [])
    if isinstance(data, list):
        return data
    return []


def save_knowledge_cards(cards: List[Dict[str, Any]]) -> None:
    """Сохраняет knowledge cards."""
    _save_json(KNOWLEDGE_CARDS_PATH, cards)


def upsert_knowledge_cards(
    new_cards: List[Dict[str, Any]],
    *,
    dialog_id: Optional[str] = None,
    source: str = "history_learning",
) -> Tuple[int, int]:
    """
    Upsert карточек в общий файл с улучшенной логикой.
    
    Args:
        new_cards: Список новых карточек
        dialog_id: ID диалога (опционально)
        source: Источник данных ("history_learning", "admin_manual", "llm_extraction")
        
    Returns:
        (created_count, updated_count)
    """
    if not new_cards:
        return 0, 0

    cards = load_knowledge_cards()
    index: Dict[str, int] = {}
    for i, c in enumerate(cards):
        topic_key = _norm(c.get("topic", ""))
        if topic_key:
            index[topic_key] = i

    created = 0
    updated = 0
    now = datetime.now().isoformat()

    for c in new_cards:
        topic = (c.get("topic") or "").strip()
        if not topic:
            continue
        topic_key = _norm(topic)
        facts_in = c.get("facts") or []
        if not isinstance(facts_in, list):
            facts_in = []
        facts_in = [str(x).strip() for x in facts_in if str(x).strip()]
        tags_in = c.get("tags") or []
        if not isinstance(tags_in, list):
            tags_in = []
        tags_in = [str(x).strip() for x in tags_in if str(x).strip()]
        category = (c.get("category") or "общее").strip()
        priority = c.get("priority", PRIORITY_MEDIUM)
        if priority not in [PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW]:
            priority = PRIORITY_MEDIUM

        if topic_key in index:
            existing = cards[index[topic_key]]
            facts_old = existing.get("facts") or []
            if not isinstance(facts_old, list):
                facts_old = []
            merged_facts = sorted(set([str(x).strip() for x in facts_old if str(x).strip()] + facts_in))

            tags_old = existing.get("tags") or []
            if not isinstance(tags_old, list):
                tags_old = []
            merged_tags = sorted(set([str(x).strip() for x in tags_old if str(x).strip()] + tags_in))

            existing["topic"] = topic
            existing["facts"] = merged_facts
            if merged_tags:
                existing["tags"] = merged_tags
            if category and category in CATEGORIES:
                existing["category"] = category
            # Обновляем приоритет только если новый выше
            if priority < existing.get("priority", PRIORITY_MEDIUM):
                existing["priority"] = priority
            existing["source"] = existing.get("source") or source
            existing["updated_at"] = now

            if dialog_id:
                dialog_ids = existing.get("dialog_ids") or []
                if not isinstance(dialog_ids, list):
                    dialog_ids = []
                if dialog_id not in dialog_ids:
                    dialog_ids.append(dialog_id)
                existing["dialog_ids"] = dialog_ids

            updated += 1
        else:
            out = {
                "topic": topic,
                "category": category if category in CATEGORIES else "общее",
                "facts": facts_in,
                "tags": tags_in,
                "priority": priority,
                "relevance_score": 0.5,  # Начальная релевантность
                "source": source,
                "dialog_ids": [dialog_id] if dialog_id else [],
                "usage_count": 0,
                "created_at": now,
                "updated_at": now,
            }
            cards.append(out)
            index[topic_key] = len(cards) - 1
            created += 1

    if created or updated:
        save_knowledge_cards(cards)
        logger.info("Knowledge cards upsert: created=%d updated=%d", created, updated)
    return created, updated


def search_knowledge_cards(
    query: str,
    *,
    limit: int = 10,
    category: Optional[str] = None,
    min_relevance: float = 0.3
) -> List[Tuple[float, Dict[str, Any]]]:
    """
    Улучшенный семантический поиск по knowledge cards с ранжированием.
    
    Использует комбинацию методов:
    - Точное совпадение в теме
    - Частичное совпадение в фактах
    - Совпадение по тегам
    - Семантическая схожесть
    
    Args:
        query: Поисковый запрос
        limit: Максимальное количество результатов
        category: Фильтр по категории (опционально)
        min_relevance: Минимальная релевантность (0-1)
        
    Returns:
        Список кортежей (score, card) отсортированных по релевантности
    """
    if not query:
        return []
    
    q_norm = _norm(query)
    q_words = set(word for word in q_norm.split() if len(word) > 2)
    
    cards = load_knowledge_cards()
    scored: List[Tuple[float, Dict[str, Any]]] = []
    
    for card in cards:
        # Фильтр по категории
        if category and card.get("category") != category:
            continue
        
        topic = card.get("topic", "")
        facts = card.get("facts", [])
        tags = card.get("tags", [])
        priority = card.get("priority", PRIORITY_MEDIUM)
        relevance_score = card.get("relevance_score", 0.5)
        usage_count = card.get("usage_count", 0)
        
        # Вычисляем релевантность
        score = 0.0
        
        # 1. Точное совпадение в теме (высокий вес)
        topic_norm = _norm(topic)
        if q_norm in topic_norm:
            score += 0.5
        elif topic_norm in q_norm:
            score += 0.3
        
        # 2. Семантическая схожесть темы
        topic_similarity = difflib.SequenceMatcher(None, q_norm, topic_norm).ratio()
        score += topic_similarity * 0.3
        
        # 3. Совпадение по фактам
        facts_text = " ".join([str(f) for f in facts])
        facts_norm = _norm(facts_text)
        facts_similarity = difflib.SequenceMatcher(None, q_norm, facts_norm).ratio()
        score += facts_similarity * 0.2
        
        # 4. Совпадение по тегам
        tag_matches = sum(1 for tag in tags if _norm(str(tag)) in q_norm or q_norm in _norm(str(tag)))
        if tag_matches > 0:
            score += min(0.2, tag_matches * 0.1)
        
        # 5. Совпадение ключевых слов
        topic_words = set(word for word in topic_norm.split() if len(word) > 2)
        common_words = q_words & topic_words
        if common_words:
            score += min(0.15, len(common_words) * 0.05)
        
        # 6. Бонусы за приоритет и использование
        if priority == PRIORITY_HIGH:
            score *= 1.2
        if usage_count > 0:
            score *= 1.1
        
        # 7. Учитываем сохраненную релевантность
        score = (score * 0.7) + (relevance_score * 0.3)
        
        if score >= min_relevance:
            scored.append((score, card))
    
    # Сортируем по релевантности
    scored.sort(key=lambda x: x[0], reverse=True)
    
    return scored[:limit]


def find_cards(query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Простой поиск (для обратной совместимости).
    
    Returns:
        Список карточек без scores
    """
    results = search_knowledge_cards(query, limit=limit)
    return [card for _, card in results]


def get_card_by_topic(topic: str) -> Optional[Dict[str, Any]]:
    """Получает карточку по точному названию темы."""
    t = (topic or "").strip()
    if not t:
        return None
    t_key = _norm(t)
    for c in load_knowledge_cards():
        if _norm(c.get("topic", "")) == t_key:
            return c
    return None


def add_facts(
    topic: str,
    facts: List[str],
    *,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
    priority: int = PRIORITY_MEDIUM,
    source: str = "admin_manual",
    dialog_id: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Добавляет факты к существующей теме (или создаёт новую карточку).
    
    Args:
        topic: Название темы
        facts: Список фактов
        category: Категория (опционально)
        tags: Теги (опционально)
        priority: Приоритет (1-высокий, 2-средний, 3-низкий)
        source: Источник данных
        dialog_id: ID диалога (опционально)
        
    Returns:
        (success, message)
    """
    t = (topic or "").strip()
    if not t:
        return False, "Пустой topic."
    facts_in = [str(x).strip() for x in (facts or []) if str(x).strip()]
    if not facts_in:
        return False, "Пустые facts."
    
    card_data = {
        "topic": t,
        "facts": facts_in,
        "tags": tags or [],
        "category": category or "общее",
        "priority": priority
    }
    
    created, updated = upsert_knowledge_cards([card_data], dialog_id=dialog_id, source=source)
    if created or updated:
        return True, f"Готово: created={created}, updated={updated}"
    return False, "Не удалось сохранить карточку."


def add_knowledge_from_text(
    text: str,
    *,
    source: str = "admin_manual",
    dialog_id: Optional[str] = None
) -> Tuple[int, List[str]]:
    """
    Извлекает знания из текста и создает knowledge cards.
    
    Парсит текст и пытается извлечь темы и факты.
    Поддерживает структурированный текст с заголовками и списками.
    
    Args:
        text: Текст для обработки
        source: Источник данных
        dialog_id: ID диалога (опционально)
        
    Returns:
        (count, topics) - количество созданных карточек и список тем
    """
    if not text or not text.strip():
        return 0, []
    
    cards = []
    topics = []
    
    # Разбиваем на абзацы
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    
    current_topic = None
    current_facts = []
    current_country = None  # Страна для текущей темы
    
    for para in paragraphs:
        # Проверяем, является ли абзац заголовком (короткий, без точек, может быть с двоеточием)
        para_clean = para.strip()
        is_header = (
            len(para_clean) < 100 and
            (para_clean.endswith(":") or
             not "." in para_clean[:50] or
             para_clean.startswith("#") or
             para_clean.isupper())
        )
        
        if is_header:
            # Сохраняем предыдущую тему
            if current_topic and current_facts:
                # Добавляем страну в тему, если она найдена
                final_topic = _enrich_topic_with_country(current_topic, current_country, current_facts)
                cards.append({
                    "topic": final_topic,
                    "facts": current_facts,
                    "tags": _extract_tags_from_text(final_topic + " " + " ".join(current_facts)),
                    "category": _guess_category(final_topic),
                    "priority": PRIORITY_MEDIUM
                })
                topics.append(final_topic)
            
            # Новая тема
            current_topic = para_clean.replace("#", "").replace(":", "").strip()
            current_facts = []
            current_country = _extract_country_from_text(current_topic)
        else:
            # Это факт для текущей темы
            if current_topic:
                # Извлекаем факты из списка или текста
                if para_clean.startswith("-") or para_clean.startswith("•"):
                    facts = [f.strip().lstrip("-•").strip() for f in para_clean.split("\n") if f.strip()]
                    current_facts.extend(facts)
                else:
                    current_facts.append(para_clean)
                
                # Обновляем страну, если она найдена в фактах
                if not current_country:
                    current_country = _extract_country_from_text(para_clean)
            else:
                # Если нет темы, создаем общую тему
                if not current_topic:
                    current_topic = "Общая информация"
                current_facts.append(para_clean)
                # Пробуем найти страну в тексте
                if not current_country:
                    current_country = _extract_country_from_text(para_clean)
    
    # Сохраняем последнюю тему
    if current_topic and current_facts:
        # Добавляем страну в тему, если она найдена
        final_topic = _enrich_topic_with_country(current_topic, current_country, current_facts)
        cards.append({
            "topic": final_topic,
            "facts": current_facts,
            "tags": _extract_tags_from_text(final_topic + " " + " ".join(current_facts)),
            "category": _guess_category(final_topic),
            "priority": PRIORITY_MEDIUM
        })
        topics.append(final_topic)
    
    if cards:
        created, updated = upsert_knowledge_cards(cards, dialog_id=dialog_id, source=source)
        return created + updated, topics
    
    return 0, []


# Список стран для извлечения
COUNTRIES = {
    "италия": ["италия", "итальянск", "италию", "италией"],
    "греция": ["греция", "греческ", "грецию", "грецией"],
    "франция": ["франция", "французск", "францию", "францией"],
    "испания": ["испания", "испанск", "испанию", "испанией"],
    "болгария": ["болгария", "болгарск", "болгарию", "болгарией"],
    "великобритания": ["великобритания", "англия", "английск", "великобританию", "англию"],
    "сша": ["сша", "америк", "соединенные штаты", "соединенных штатов"],
    "япония": ["япония", "японск", "японию", "японией"],
    "швейцария": ["швейцария", "швейцарск", "швейцарию", "швейцарией"],
    "германия": ["германия", "немецк", "германию", "германией"],
    "австрия": ["австрия", "австрийск", "австрию", "австрией"],
    "чехия": ["чехия", "чешск", "чехию", "чехией"],
    "польша": ["польша", "польск", "польшу", "польшей"],
    "португалия": ["португалия", "португальск", "португалию", "португалией"],
    "нидерланды": ["нидерланды", "голландия", "голландск", "нидерландов"],
    "бельгия": ["бельгия", "бельгийск", "бельгию", "бельгией"],
    "дания": ["дания", "датск", "данию", "данией"],
    "швеция": ["швеция", "шведск", "швецию", "швецией"],
    "норвегия": ["норвегия", "норвежск", "норвегию", "норвегией"],
    "финляндия": ["финляндия", "финск", "финляндию", "финляндией"],
    "исландия": ["исландия", "исландск", "исландию", "исландией"],
    "мальта": ["мальта", "мальтийск", "мальту", "мальтой"],
    "кипр": ["кипр", "кипрск", "кипре", "кипром"],
}


def _extract_country_from_text(text: str) -> Optional[str]:
    """Извлекает название страны из текста."""
    text_lower = _norm(text)
    
    for country, patterns in COUNTRIES.items():
        if any(pattern in text_lower for pattern in patterns):
            return country
    
    return None


def _enrich_topic_with_country(topic: str, country: Optional[str], facts: List[str]) -> str:
    """
    Обогащает тему названием страны, если она найдена, но не указана в теме.
    
    Args:
        topic: Текущая тема
        country: Найденная страна (если есть)
        facts: Список фактов (для дополнительного поиска страны)
        
    Returns:
        Обогащенная тема с указанием страны
    """
    if not topic:
        return topic
    
    topic_lower = _norm(topic)
    
    # Проверяем, есть ли уже страна в теме
    topic_has_country = any(
        any(pattern in topic_lower for pattern in patterns)
        for patterns in COUNTRIES.values()
    )
    
    # Если страна не указана в теме, но найдена в тексте - добавляем
    if not topic_has_country and country:
        # Определяем, куда добавить страну
        country_name = country.capitalize()
        
        # Если тема начинается с общих слов - заменяем их
        if topic_lower.startswith("требования к документам"):
            return f"Требования к документам для визы в {country_name}"
        elif topic_lower.startswith("стоимость"):
            if "виза" in topic_lower:
                return f"Стоимость визы в {country_name}"
            else:
                return f"Стоимость визы в {country_name}"
        elif topic_lower.startswith("сроки") or topic_lower.startswith("срок"):
            if "рассмотрен" in topic_lower or "оформлен" in topic_lower:
                return f"Сроки рассмотрения визы в {country_name}"
            else:
                return f"Сроки оформления визы в {country_name}"
        elif topic_lower.startswith("особые условия") or topic_lower.startswith("условия"):
            return f"Особые условия для визы в {country_name}"
        elif topic_lower.startswith("документы"):
            return f"Документы для визы в {country_name}"
        elif "виза" in topic_lower:
            # Если в теме есть слово "виза", добавляем страну после него
            return f"{topic} в {country_name}"
        else:
            # Общий случай - добавляем в конец
            return f"{topic} (виза в {country_name})"
    
    return topic


def _extract_tags_from_text(text: str) -> List[str]:
    """Извлекает теги из текста."""
    text_lower = _norm(text)
    tags = []
    
    # Извлекаем страну
    country = _extract_country_from_text(text)
    if country:
        tags.append(country)
    
    # Ключевые слова для тегов
    keywords = {
        "виза": ["виза", "визы", "визовый"],
        "шенген": ["шенген", "шенгенская"],
        "документы": ["документ", "паспорт", "справка"],
        "стоимость": ["стоимость", "цена", "тариф", "оплат"],
        "сроки": ["срок", "день", "недел", "месяц"]
    }
    
    for tag, patterns in keywords.items():
        if any(pattern in text_lower for pattern in patterns):
            tags.append(tag)
    
    return tags


def _guess_category(topic: str) -> str:
    """Определяет категорию по теме."""
    topic_lower = _norm(topic)
    
    if any(word in topic_lower for word in ["виза", "шенген", "италия", "греция", "франция", "испания"]):
        if "шенген" in topic_lower or any(c in topic_lower for c in ["италия", "греция", "франция", "испания"]):
            return "визы_шенген"
        return "визы_другие"
    elif any(word in topic_lower for word in ["документ", "паспорт", "справка"]):
        return "документы"
    elif any(word in topic_lower for word in ["стоимость", "цена", "тариф", "оплат"]):
        return "стоимость"
    elif any(word in topic_lower for word in ["срок", "день", "недел", "месяц"]):
        return "сроки"
    elif any(word in topic_lower for word in ["процесс", "оформлен", "подач"]):
        return "процесс"
    elif any(word in topic_lower for word in ["условие", "ограничен", "особ"]):
        return "особые_условия"
    
    return "общее"


def update_usage(card: Dict[str, Any]) -> None:
    """Обновляет статистику использования карточки."""
    cards = load_knowledge_cards()
    topic_key = _norm(card.get("topic", ""))
    
    for c in cards:
        if _norm(c.get("topic", "")) == topic_key:
            c["usage_count"] = c.get("usage_count", 0) + 1
            c["last_used_at"] = datetime.now().isoformat()
            # Увеличиваем релевантность при использовании
            current_relevance = c.get("relevance_score", 0.5)
            c["relevance_score"] = min(1.0, current_relevance + 0.01)
            break
    
    save_knowledge_cards(cards)


def delete_card(topic: str) -> Tuple[bool, str]:
    """Удаляет карточку по теме."""
    t = (topic or "").strip()
    if not t:
        return False, "Пустой topic."
    t_key = _norm(t)
    cards = load_knowledge_cards()
    before = len(cards)
    cards = [c for c in cards if _norm(c.get("topic", "")) != t_key]
    if len(cards) == before:
        return False, "Тема не найдена."
    save_knowledge_cards(cards)
    return True, "Карточка удалена."


def merge_topics(from_topic: str, into_topic: str, *, source: str = "admin_merge", dialog_id: Optional[str] = None) -> Tuple[bool, str]:
    """Склеивает 2 темы."""
    a = (from_topic or "").strip()
    b = (into_topic or "").strip()
    if not a or not b:
        return False, "Нужны обе темы."
    if _norm(a) == _norm(b):
        return False, "Темы совпадают."

    cards = load_knowledge_cards()
    idx_a = None
    idx_b = None
    for i, c in enumerate(cards):
        if _norm(c.get("topic", "")) == _norm(a):
            idx_a = i
        if _norm(c.get("topic", "")) == _norm(b):
            idx_b = i
    if idx_a is None:
        return False, f"Не найдена тема: {a}"
    if idx_b is None:
        return False, f"Не найдена тема: {b}"

    ca = cards[idx_a]
    cb = cards[idx_b]
    facts_a = ca.get("facts") or []
    tags_a = ca.get("tags") or []
    dialog_ids_a = ca.get("dialog_ids") or []
    facts_a = [str(x).strip() for x in facts_a] if isinstance(facts_a, list) else [str(facts_a).strip()]
    tags_a = [str(x).strip() for x in tags_a] if isinstance(tags_a, list) else []
    dialog_ids_a = [str(x).strip() for x in dialog_ids_a] if isinstance(dialog_ids_a, list) else []

    ok, msg = add_facts(b, facts_a, tags=tags_a, source=source, dialog_id=dialog_id)
    if not ok:
        return False, msg

    # Перенесём tags/dialog_ids
    cards = load_knowledge_cards()
    for c in cards:
        if _norm(c.get("topic", "")) == _norm(b):
            tags_old = c.get("tags") or []
            if not isinstance(tags_old, list):
                tags_old = []
            c["tags"] = sorted(set([str(x).strip() for x in tags_old if str(x).strip()] + [x for x in tags_a if x]))
            d_old = c.get("dialog_ids") or []
            if not isinstance(d_old, list):
                d_old = []
            d_merged = sorted(set([str(x).strip() for x in d_old if str(x).strip()] + [x for x in dialog_ids_a if x]))
            if dialog_id:
                d_merged = sorted(set(d_merged + [dialog_id]))
            c["dialog_ids"] = d_merged
            c["updated_at"] = datetime.now().isoformat()
            c["source"] = c.get("source") or source
            break

    cards = [c for c in cards if _norm(c.get("topic", "")) != _norm(a)]
    save_knowledge_cards(cards)
    return True, f"Склеено: '{a}' → '{b}'"


def list_recent_cards(*, limit: int = 10) -> List[Dict[str, Any]]:
    """Возвращает недавно обновленные карточки."""
    cards = load_knowledge_cards()
    def key(c: Dict[str, Any]) -> str:
        return str(c.get("updated_at") or c.get("created_at") or "")
    cards.sort(key=key, reverse=True)
    return cards[:limit]
