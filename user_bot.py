"""
Модуль обработчиков команд и сообщений для Telegram бота.

Содержит обработчики для:
- Команд пользователей (/start)
- Административных команд (/knowledge, /kb, /staticcontext, /dynamiccontext, /systemprompt, /subscribe, /unsubscribe)
- Обычных сообщений пользователей (генерация ответов через LLM)
- Управления базой знаний (knowledge cards) через /knowledge или /kb
"""
import os
import random
import time
import json
import logging
import re
import httpx
import shutil
from datetime import datetime
from typing import Dict, Any, List, Optional
from aiogram import F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand, FSInputFile
from aiogram.filters import CommandStart
from aiogram.utils.chat_action import ChatActionSender
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import AsyncOpenAI
from bs4 import BeautifulSoup

from create_bot import bot
from config import (
    FAQ_PATH, KNOWLEDGE_CARDS_PATH, STATIC_CONTEXT_PATH, DYNAMIC_CONTEXT_PATH, SYSTEM_PROMPT_PATH, CHAT_HISTORY_PATH,
    LLM_MODEL, TEMPERATURE, OPENAI_API_KEY, ADMINS, WEBHOOK_URL, DATA_DIR, SIGNAL_PHRASES,
    MANAGER_COST_PER_HOUR, USD_RATE, get_bot_version
)
from avito_sessions import (
    set_bot_enabled, is_bot_enabled, get_llm_model, set_llm_model,
    get_bot_mode, set_bot_mode, get_partial_percentage, set_partial_percentage,
    BOT_MODE_LISTENING, BOT_MODE_PARTIAL, BOT_MODE_FULL
)
from responder import generate_reply
from avito_api import subscribe_webhook, unsubscribe_webhook
from utils.chat_history import save_assistant_message
from utils.faq_utils import load_faq_safe
from utils.knowledge_cards import (
    upsert_knowledge_cards,
    load_knowledge_cards,
    find_cards,
    delete_card,
    add_facts,
    merge_topics,
    list_recent_cards,
    add_knowledge_from_text,
    search_knowledge_cards,
)
from utils.stats import calculate_stats, calculate_token_cost, calculate_account_stats
from utils.avito_accounts import (
    list_accounts,
    get_account as get_avito_account,
    upsert_account as upsert_avito_account,
    set_paused as set_avito_account_paused,
    delete_account as delete_avito_account,
    set_mode as set_avito_account_mode,
    set_account_credentials as set_avito_account_credentials,
)
from avito_api import get_subscriptions

# Константы
MAX_FAQ_CHUNK_SIZE: int = 6000
SYSTEM_MESSAGE_PREFIXES: List[str] = ["Системное:", "Сообщение отправлено"]
DIALOG_ID_PATTERN: re.Pattern = re.compile(r";([0-9]+:m:[^:]+):")
DIALOG_ID_CLEANUP_PATTERN: re.Pattern = re.compile(r"[a-z0-9]+;[0-9]+:m:[^:]+:[0-9]+$")
NAME_PATTERN: re.Pattern = re.compile(r"^([\wА-Яа-яёЁ]+):\s*(.+)")
SUBSCRIBE_PATTERN: re.Pattern = re.compile(r"^/subscribe\b")
UNSUBSCRIBE_PATTERN: re.Pattern = re.compile(r"^/unsubscribe\b")


def _extract_json_array(text: str) -> Optional[str]:
    """
    Пытается аккуратно извлечь JSON-массив из текста (включая ```json fences).
    Возвращает строку JSON или None.
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        parts = s.split("```")
        if len(parts) >= 2:
            s = parts[1].strip()
            if s.startswith("json"):
                s = s[4:].strip()
    m = re.search(r"\[[\s\S]*\]", s)
    if m:
        return m.group(0).strip()
    return None


def _fallback_cards_from_text(raw_text: str) -> List[Dict[str, Any]]:
    """
    Фоллбек без LLM: всегда пытается сделать хотя бы 1 карточку из текста.
    """
    t = (raw_text or "").strip()
    if not t:
        return []

    # Пробуем вытащить тему вида "виза в <страна/город>"
    topic = ""
    m = re.search(r"(виз[ауыеи]\s+в\s+)([а-яё\s\-]+)", t.lower(), re.IGNORECASE)
    if m:
        tail = m.group(2).strip()
        # обрежем по типичным стоп-словам
        tail = re.split(r"\b(не|нет|сто(ит|имость)|цена|срок|если)\b", tail, maxsplit=1)[0].strip()
        tail = re.sub(r"\s+", " ", tail).strip(" .,-")
        if tail:
            topic = f"Виза в {tail}".strip()
            # простая нормализация регистра
            topic = topic[0].upper() + topic[1:]

    if not topic:
        # иначе тема = первая фраза/предложение
        first = re.split(r"[.!?\n]+", t, maxsplit=1)[0].strip()
        topic = first[:120] if first else t[:120]

    # facts = предложения/строки
    parts = [p.strip() for p in re.split(r"[.\n]+", t) if p.strip()]
    facts = []
    for p in parts[:10]:
        p = re.sub(r"\s+", " ", p).strip()
        if p:
            facts.append(p)
    if not facts:
        facts = [t]

    return [{"topic": topic, "facts": facts, "tags": []}]


async def _knowledge_cards_from_text_via_llm(raw_text: str) -> List[Dict[str, Any]]:
    """
    Превращает произвольный текст в knowledge cards через LLM.
    """
    if not raw_text or not raw_text.strip():
        return []

    # Если LLM не настроен — вернём фоллбек
    if not OPENAI_API_KEY or not client:
        return _fallback_cards_from_text(raw_text)

    # Защита от слишком длинных кусков
    chunks = [raw_text[i:i + MAX_FAQ_CHUNK_SIZE] for i in range(0, len(raw_text), MAX_FAQ_CHUNK_SIZE)]
    model = get_llm_model(LLM_MODEL)
    use_temperature = model not in ["gpt-5-mini", "gpt-5"]

    all_cards: List[Dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        prompt = f"""Ты — помощник, который структурирует базу знаний для визового сервиса.

Вход: произвольный текст (часть {idx} из {len(chunks)}).

ЗАДАЧА:
Сформируй *минимальное* количество knowledge cards из текста.

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
- Если в тексте НЕ упоминается конкретная страна, но есть общая информация - используй формат:
  * "Общие требования к документам для шенгенских виз"
  * "Общая стоимость визовых услуг"
- Страны, которые могут упоминаться: Италия, Греция, Франция, Испания, Болгария, Великобритания, США, Япония, Швейцария, Германия, Австрия, Чехия, Польша, Португалия, Нидерланды, Бельгия, Дания, Швеция, Норвегия, Финляндия, Исландия, Мальта, Кипр, Лихтенштейн, Люксембург, Словения, Словакия, Венгрия, Эстония, Латвия, Литва

ЧТО ИЗВЛЕКАТЬ (стабильная информация):
✅ Общие требования к документам (список документов, требования к оформлению)
✅ Процессы оформления виз (как подавать, куда обращаться)
✅ Общие условия и правила (особенности для разных стран, ограничения)
✅ Стабильные правила работы (условия оплаты, возврата, общие принципы)
✅ Структура услуг (типы виз, категории услуг)
✅ **МАНЕРА ОБЩЕНИЯ** - это САМОЕ ВАЖНОЕ! Извлекай примеры того, как менеджер общается:
  - Примеры фраз и формулировок (как приветствует, как объясняет, как успокаивает)
  - Стиль ответов (простой, человечный язык, без канцелярита)
  - Тон общения (доброжелательный, спокойный, уверенный)
  - Как объясняются сложные вещи простым языком
  - Как предлагается помощь
  - Использование эмодзи (если есть)
  - Структура ответов (коротко, по делу, с конкретикой)
  - Создавай карточки категории "манера_общения" с примерами фраз

ЧТО НЕ ИЗВЛЕКАТЬ (динамическая информация, берется из динамического контекста):
❌ Конкретные даты записи (например, "запись на 15 декабря", "свободные даты на следующую неделю")
❌ Актуальные цены и тарифы (например, "стоимость 50000 рублей", "цена 3000 евро")
❌ Текущие сроки рассмотрения (например, "сейчас рассматривают 45 дней", "на данный момент 2 недели")
❌ Доступность записей (например, "запись доступна на декабрь", "можно записаться на следующую неделю")
❌ Процент одобрения на текущий момент
❌ Любая информация, которая может измениться в ближайшее время

ПРАВИЛА:
- Не выдумывай факты — используй только то, что явно есть в тексте.
- Извлекай только стабильную информацию, которая не меняется часто.
- НЕ извлекай динамическую информацию (даты, цены, сроки) - она хранится в динамическом контексте.
- Если в тексте только динамическая информация — НЕ создавай карточки.
- Follow-up детали об одной теме объединяй в ОДНУ карточку.
- Facts пиши коротко, конкретно, по одному факту на строку.
- ВСЕГДА включай страну в тему, если она упоминается в тексте.

ФОРМАТ (строго JSON-массив):
[
  {{"topic": "...", "facts": ["...", "..."], "tags": ["...", "название_страны"]}},
  ...
]

ТЕКСТ:
{chunk}
"""
        try:
            if use_temperature:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
            else:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                )

            content = (resp.choices[0].message.content or "").strip()
            json_arr = _extract_json_array(content)
            if not json_arr:
                continue
            parsed = json.loads(json_arr)
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                topic = (item.get("topic") or "").strip()
                facts = item.get("facts") or []
                if not topic or not isinstance(facts, list) or not facts:
                    continue
                tags = item.get("tags") or []
                if not isinstance(tags, list):
                    tags = []
                all_cards.append({"topic": topic, "facts": facts, "tags": tags})
        except Exception as e:
            logger.exception("LLM error while building knowledge cards (chunk %d/%d): %s", idx, len(chunks), e)
            continue
    # Если LLM ничего не вернул/не распарсилось — не падаем, делаем фоллбек
    return all_cards if all_cards else _fallback_cards_from_text(raw_text)

# Инициализация
user_router = Router()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------------------
# UI helpers: bot mode wording
# ----------------------------
def _mode_label(mode: str, partial_percent: int) -> str:
    if mode == BOT_MODE_LISTENING:
        return "ТОЛЬКО УЧУСЬ"
    if mode == BOT_MODE_PARTIAL:
        return f"УЧУСЬ И ЧАСТИЧНО ОТВЕЧАЮ ({partial_percent}%)"
    if mode == BOT_MODE_FULL:
        return "УЧУСЬ И ПОЛНОСТЬЮ ОТВЕЧАЮ"
    return mode


def _mode_button_text(mode: str, current_mode: str, partial_percent: int) -> str:
    check = "✅ " if mode == current_mode else ""
    # ✅ должна быть только на выбранном варианте, поэтому здесь нет других ✅ в тексте
    icon = {
        BOT_MODE_LISTENING: "🧠",
        BOT_MODE_PARTIAL: "🧪",
        BOT_MODE_FULL: "🚀",
    }.get(mode, "⚙️")
    return f"{check}{icon} {_mode_label(mode, partial_percent)}"


def _build_bot_mode_menu_ui(current_mode: str, partial_percent: int) -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        [InlineKeyboardButton(text=_mode_button_text(BOT_MODE_LISTENING, current_mode, partial_percent), callback_data="bot_mode_listening")],
        [InlineKeyboardButton(text=_mode_button_text(BOT_MODE_PARTIAL, current_mode, partial_percent), callback_data="bot_mode_partial")],
        [InlineKeyboardButton(text=_mode_button_text(BOT_MODE_FULL, current_mode, partial_percent), callback_data="bot_mode_full")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="bot_status_back")],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = (
        "⚙️ <b>Режим работы бота</b>\n\n"
        "🧠 <b>ТОЛЬКО УЧУСЬ</b> — бот только читает переписки и формирует базу знаний из истории.\n"
        "   Не отвечает на сообщения.\n\n"
        f"🧪 <b>УЧУСЬ И ЧАСТИЧНО ОТВЕЧАЮ</b> — бот отвечает на часть сообщений (для тестирования).\n"
        f"   Текущий процент: <b>{partial_percent}%</b>\n\n"
        "🚀 <b>УЧУСЬ И ПОЛНОСТЬЮ ОТВЕЧАЮ</b> — бот отвечает всем (рабочий режим).\n"
        "   Если не может ответить — передает менеджеру.\n\n"
        f"Текущий режим: <b>{_mode_label(current_mode, partial_percent)}</b>"
    )
    return text, keyboard


# ----------------------------
# UI helpers: per-account mode wording
# ----------------------------
def _acc_mode_label(mode: str, partial_percent: int) -> str:
    if mode == BOT_MODE_LISTENING:
        return "УЧУСЬ (не отвечаю)"
    if mode == BOT_MODE_PARTIAL:
        return f"УЧУСЬ + ОТВЕЧАЮ ЧАСТИЧНО ({partial_percent}%)"
    if mode == BOT_MODE_FULL:
        return "РАБОТАЮ ПОЛНОСТЬЮ"
    return str(mode)


def _acc_mode_button_text(mode: str, current_mode: str, partial_percent: int) -> str:
    check = "✅ " if mode == current_mode else ""
    icon = {
        BOT_MODE_LISTENING: "🧠",
        BOT_MODE_PARTIAL: "🧪",
        BOT_MODE_FULL: "🚀",
    }.get(mode, "⚙️")
    return f"{check}{icon} {_acc_mode_label(mode, partial_percent)}"


def _account_status_text(acc: Dict[str, Any]) -> str:
    aid = str(acc.get("account_id") or "").strip()
    name = (acc.get("name") or "").strip()
    paused = bool(acc.get("paused", False))
    mode = (acc.get("mode") or BOT_MODE_FULL).strip()
    partial = int(acc.get("partial_percentage", 50) or 50)
    has_creds = bool((acc.get("client_id") or "").strip() and (acc.get("client_secret") or "").strip())
    paused_txt = "⏸ ПАУЗА" if paused else "▶️ АКТИВЕН"
    title = f"{aid}" + (f" — {name}" if name else "")
    return (
        f"🧾 <b>Avito аккаунт</b>\n"
        f"• <b>{title}</b>\n"
        f"• Креды: <b>{'✅ настроены' if has_creds else '❌ нет client_id/secret'}</b>\n"
        f"• Статус: <b>{paused_txt}</b>\n"
        f"• Режим: <b>{_acc_mode_label(mode, partial)}</b>"
    )


async def _safe_edit_text(message: Message, text: str, *, reply_markup: Optional[InlineKeyboardMarkup] = None, parse_mode: Optional[str] = None) -> None:
    """
    Telegram иногда возвращает "message is not modified" если контент/кнопки не изменились.
    Это не ошибка для пользователя — просто игнорируем.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise


def _unique_avito_app_creds() -> List[Dict[str, str]]:
    """
    Возвращает список уникальных наборов (client_id, client_secret) из avito_accounts.json.
    """
    seen = set()
    out: List[Dict[str, str]] = []
    for a in list_accounts():
        cid = str(a.get("client_id") or "").strip()
        csec = str(a.get("client_secret") or "").strip()
        if not cid or not csec:
            continue
        key = (cid, csec)
        if key in seen:
            continue
        seen.add(key)
        out.append({"client_id": cid, "client_secret": csec})
    return out


def _mask_secret(s: str, *, keep: int = 4) -> str:
    s = str(s or "")
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def _get_account_creds(account_id: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Возвращает (client_id, client_secret, error_msg).
    """
    acc = get_avito_account(account_id) or {}
    cid = str(acc.get("client_id") or "").strip()
    csec = str(acc.get("client_secret") or "").strip()
    if not cid or not csec:
        return None, None, "❌ Для аккаунта не заполнены client_id/client_secret (зайдите в /accounts → Добавить заново или пришлите креды)."
    return cid, csec, None

# Инициализация OpenAI клиента
http_client = httpx.AsyncClient()
client = AsyncOpenAI(api_key=OPENAI_API_KEY, http_client=http_client)

# Инициализация директории данных
os.makedirs(DATA_DIR, exist_ok=True)

# Инициализация файлов
if not os.path.exists(FAQ_PATH):
    with open(FAQ_PATH, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)
if not os.path.exists(STATIC_CONTEXT_PATH):
    with open(STATIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
        f.write("")

class AdminStates(StatesGroup):
    """Состояния FSM для административных команд."""
    waiting_for_faq_file = State()  # Legacy, для обратной совместимости
    waiting_for_faq_text = State()  # Legacy, для обратной совместимости
    waiting_for_knowledge_file = State()  # Загрузка файлов с переписками
    waiting_for_static_context = State()
    waiting_for_dynamic_context = State()
    waiting_for_system_prompt = State()
    waiting_for_partial_percentage = State()  # Ожидание процента для режима partial
    waiting_for_knowledge_search = State()
    waiting_for_knowledge_view = State()
    waiting_for_knowledge_delete = State()
    waiting_for_knowledge_add_fact_topic = State()
    waiting_for_knowledge_add_fact_text = State()
    waiting_for_knowledge_merge_from = State()
    waiting_for_knowledge_merge_into = State()
    waiting_for_knowledge_add_text = State()  # Добавление знаний из текста
    waiting_for_avito_account_add = State()  # legacy (kept for backwards compat)
    waiting_for_avito_account_add_account_id = State()
    waiting_for_avito_account_add_client_id = State()
    waiting_for_avito_account_add_client_secret = State()
    waiting_for_avito_account_add_name = State()
    waiting_for_avito_account_partial_percentage = State()


def _check_admin(user_id: int) -> bool:
    """
    Проверяет, является ли пользователь администратором.
    
    Args:
        user_id: ID пользователя в Telegram
        
    Returns:
        True если пользователь администратор, False иначе
    """
    return user_id in ADMINS if ADMINS else False


def _calculate_stats() -> Dict[str, Any]:
    """
    Вычисляет статистику работы бота на основе chat_history.json и FAQ.
    
    Returns:
        Словарь со статистикой:
        - total_chats: количество чатов Avito, в которых отвечал бот или менеджер
        - total_bot_responses: количество ответов бота (по role="assistant")
        - total_manager_responses: количество ответов менеджера (по role="manager")
        - total_responses: общее количество ответов (бот + менеджер)
        - bot_response_rate: доля ответов бота от всех ответов (%)
        - manager_response_rate: доля ответов менеджера от всех ответов (%)
        - manager_transfers: количество ответов бота, которые перешли на менеджера
        - manager_transfer_rate: доля ответов бота, перешедших на менеджера (%)
        - bot_finished_dialogs: количество диалогов, завершенных разговором с ботом
        - manager_finished_dialogs: количество диалогов, завершенных разговором с менеджером
        - bot_finish_rate: доля диалогов, завершенных разговором с ботом (%)
        - manager_finish_rate: доля диалогов, завершенных разговором с менеджером (%)
        - faq_total: общее количество записей в базе знаний (legacy, для обратной совместимости)
        - faq_admin: количество записей, добавленных админом (legacy)
        - faq_manager: количество записей, добавленных менеджером (legacy)
        - faq_manager_like: количество записей, лайкнутых менеджером (legacy)
    """
    try:
        from responder import _load_json
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
    except Exception as e:
        logger.exception("Ошибка при загрузке chat_history для статистики: %s", e)
        chat_history = {}
    
    # Загружаем FAQ для статистики (используем безопасную загрузку)
    try:
        faq_data, _ = load_faq_safe()
        if not isinstance(faq_data, list):
            logger.warning("FAQ данные не являются списком, используем пустой список")
            faq_data = []
    except Exception as e:
        logger.exception("Ошибка при загрузке FAQ для статистики: %s", e)
        faq_data = []
    
    total_chats = 0
    total_bot_responses = 0
    total_manager_responses = 0
    manager_transfers = 0
    bot_finished_dialogs = 0
    manager_finished_dialogs = 0
    
    # Статистика токенов
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    total_cost_usd = 0.0
    
    # Статистика времени ответа менеджера
    manager_response_times = []  # Список времен ответа в секундах
    
    # Сигнальная фраза, которая заменяет ответ при переходе на менеджера
    manager_signal_phrase = "Подождите, пожалуйста, уточняю информацию"
    
    # Обрабатываем только чаты Avito (начинаются с "avito_")
    for dialog_id, messages in chat_history.items():
        if not dialog_id.startswith("avito_"):
            continue
        
        if not isinstance(messages, list):
            continue
        
        # Подсчитываем ответы бота и менеджера в этом чате
        bot_responses_in_chat = 0
        manager_responses_in_chat = 0
        
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            
            role = msg.get("role", "")
            content = msg.get("content", "").strip()
            
            # Считаем ответы по полю role
            if role == "assistant" and content:
                bot_responses_in_chat += 1
                total_bot_responses += 1
                
                # Подсчитываем токены, если есть информация об использовании
                usage = msg.get("usage", {})
                if isinstance(usage, dict):
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    model = usage.get("model", "gpt-4o")
                    
                    if prompt_tokens > 0 or completion_tokens > 0:
                        total_prompt_tokens += prompt_tokens
                        total_completion_tokens += completion_tokens
                        total_tokens += prompt_tokens + completion_tokens
                        
                        # Рассчитываем стоимость в долларах
                        cost_usd = calculate_token_cost(model, prompt_tokens, completion_tokens)
                        total_cost_usd += cost_usd
                
                # Проверяем, является ли это переходом на менеджера
                content_lower = content.lower()
                is_manager_transfer = (
                    manager_signal_phrase.lower() in content_lower or
                    any(phrase.lower() in content_lower for phrase in SIGNAL_PHRASES)
                )
                
                if is_manager_transfer:
                    manager_transfers += 1
            elif role == "manager" and content:
                manager_responses_in_chat += 1
                total_manager_responses += 1
                
                # Рассчитываем время ответа менеджера
                manager_timestamp = msg.get("timestamp")
                if manager_timestamp:
                    try:
                        manager_time = datetime.fromisoformat(manager_timestamp)
                        
                        # Ищем предыдущее сообщение пользователя или бота (которое могло вызвать ответ менеджера)
                        msg_index = messages.index(msg)
                        for prev_msg in reversed(messages[:msg_index]):
                            if isinstance(prev_msg, dict):
                                prev_role = prev_msg.get("role", "")
                                prev_timestamp = prev_msg.get("timestamp")
                                if prev_timestamp and prev_role in ["user", "assistant"]:
                                    try:
                                        prev_time = datetime.fromisoformat(prev_timestamp)
                                        response_time_seconds = (manager_time - prev_time).total_seconds()
                                        if response_time_seconds > 0 and response_time_seconds < 86400:  # Меньше суток
                                            manager_response_times.append(response_time_seconds)
                                        break
                                    except (ValueError, TypeError):
                                        continue
                    except (ValueError, TypeError):
                        pass
        
        # Если бот или менеджер отвечали в этом чате, считаем чат
        if bot_responses_in_chat > 0 or manager_responses_in_chat > 0:
            total_chats += 1
            
            # Определяем, как завершился диалог
            if messages:
                last_msg = messages[-1]
                if isinstance(last_msg, dict):
                    last_role = last_msg.get("role", "")
                    if last_role == "manager":
                        manager_finished_dialogs += 1
                    elif last_role == "assistant":
                        last_content = last_msg.get("content", "").strip().lower()
                        is_manager_finish = (
                            manager_signal_phrase.lower() in last_content or
                            any(phrase.lower() in last_content for phrase in SIGNAL_PHRASES)
                        )
                        
                        if is_manager_finish:
                            manager_finished_dialogs += 1
                        else:
                            bot_finished_dialogs += 1
    
    # Подсчитываем статистику FAQ
    faq_total = 0
    faq_admin = 0
    faq_manager = 0
    faq_manager_like = 0
    
    if isinstance(faq_data, list):
        for item in faq_data:
            if isinstance(item, dict):
                faq_total += 1
                source = item.get("source", "")
                if source == "admin":
                    faq_admin += 1
                elif source == "manager":
                    faq_manager += 1
                elif source == "manager_like" or source == "user_like":
                    # Считаем user_like как manager_like (лайкнуто менеджером)
                    faq_manager_like += 1
    
    # Вычисляем общее количество ответов
    total_responses = total_bot_responses + total_manager_responses
    
    # Вычисляем доли
    bot_response_rate = (total_bot_responses / total_responses * 100) if total_responses > 0 else 0.0
    manager_response_rate = (total_manager_responses / total_responses * 100) if total_responses > 0 else 0.0
    manager_transfer_rate = (manager_transfers / total_bot_responses * 100) if total_bot_responses > 0 else 0.0
    total_finished_dialogs = bot_finished_dialogs + manager_finished_dialogs
    bot_finish_rate = (bot_finished_dialogs / total_finished_dialogs * 100) if total_finished_dialogs > 0 else 0.0
    manager_finish_rate = (manager_finished_dialogs / total_finished_dialogs * 100) if total_finished_dialogs > 0 else 0.0
    
    # Рассчитываем среднее время ответа менеджера
    avg_manager_response_time_seconds = 0.0
    if manager_response_times:
        avg_manager_response_time_seconds = sum(manager_response_times) / len(manager_response_times)
    avg_manager_response_time_hours = avg_manager_response_time_seconds / 3600
    
    # Рассчитываем стоимость бота в рублях
    total_cost_rub = total_cost_usd * USD_RATE
    
    # Рассчитываем сэкономленное время менеджера
    # Предполагаем, что каждый ответ бота экономит время менеджера (среднее время ответа менеджера)
    # Но учитываем только те ответы бота, которые не перешли на менеджера
    bot_responses_without_transfer = total_bot_responses - manager_transfers
    saved_time_hours = bot_responses_without_transfer * avg_manager_response_time_hours if avg_manager_response_time_hours > 0 else 0.0
    
    # Рассчитываем сэкономленные деньги менеджера
    saved_money_rub = saved_time_hours * MANAGER_COST_PER_HOUR
    
    # Чистая экономия (сэкономленные деньги минус стоимость бота)
    net_savings_rub = saved_money_rub - total_cost_rub
    
    return {
        "total_chats": total_chats,
        "total_bot_responses": total_bot_responses,
        "total_manager_responses": total_manager_responses,
        "total_responses": total_responses,
        "bot_response_rate": bot_response_rate,
        "manager_response_rate": manager_response_rate,
        "manager_transfers": manager_transfers,
        "manager_transfer_rate": manager_transfer_rate,
        "bot_finished_dialogs": bot_finished_dialogs,
        "manager_finished_dialogs": manager_finished_dialogs,
        "bot_finish_rate": bot_finish_rate,
        "manager_finish_rate": manager_finish_rate,
        "faq_total": faq_total,
        "faq_admin": faq_admin,
        "faq_manager": faq_manager,
        "faq_manager_like": faq_manager_like,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost_usd,
        "total_cost_rub": total_cost_rub,
        "avg_manager_response_time_seconds": avg_manager_response_time_seconds,
        "avg_manager_response_time_hours": avg_manager_response_time_hours,
        "saved_time_hours": saved_time_hours,
        "saved_money_rub": saved_money_rub,
        "net_savings_rub": net_savings_rub
    }


# ----------------------------
# /start
# ----------------------------
@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает команду /start.
    
    Приветствует пользователя и объясняет возможности бота.
    Также очищает любое активное состояние FSM.
    
    Args:
        message: Сообщение с командой /start
        state: FSM контекст для управления состоянием
    """
    # Очищаем состояние, если оно было активно
    await state.clear()
    
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        text = (
            "Привет! Я цифровой помощник компании VisaWay!"
        )
        await message.answer(text)
        logger.info("/start вызван пользователем %d", message.from_user.id)


# ----------------------------
# /botstatus — управление ботом
# ----------------------------
@user_router.message(F.text.regexp(r"^/botstatus\b"))
async def cmd_bot_status_menu(message: Message, state: FSMContext) -> None:
    """Показывает меню управления ботом (ON/OFF и выбор модели LLM)."""
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    await state.clear()
    
    # Определяем текущий статус
    current_status = is_bot_enabled()
    status_text = "🟢 ВКЛЮЧЕН" if current_status else "🔴 ВЫКЛЮЧЕН"
    
    # Получаем текущий режим работы
    current_mode = get_bot_mode()
    partial_percent = get_partial_percentage()
    current_mode_name = _mode_label(current_mode, partial_percent)
    
    # Получаем текущую модель LLM
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    
    # Получаем версию бота
    bot_version = get_bot_version()
    
    # Создаем inline кнопки в зависимости от текущего статуса
    buttons = []
    
    # Кнопка включения/выключения бота
    if current_status:
        buttons.append([InlineKeyboardButton(text="🔴 Выключить бота", callback_data="bot_off")])
    else:
        buttons.append([InlineKeyboardButton(text="🟢 Включить бота", callback_data="bot_on")])
    
    # Кнопки для выбора режима работы
    buttons.append([InlineKeyboardButton(text="⚙️ Режим работы бота", callback_data="bot_mode_menu")])
    
    # Кнопки для выбора модели LLM
    buttons.append([InlineKeyboardButton(text="🤖 Выбрать модель LLM", callback_data="llm_model_menu")])
    
    # Кнопки для webhook
    buttons.append([
        InlineKeyboardButton(text="🔗 Подключить webhook", callback_data="webhook_subscribe"),
        InlineKeyboardButton(text="🔌 Отключить webhook", callback_data="webhook_unsubscribe"),
    ])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    mode_info = f"📊 Режим: {current_mode_name}"
    
    await message.answer(
        f"🤖 Управление ботом\n\n"
        f"📊 Текущий статус бота: {status_text}\n"
        f"⚙️ Режим работы: <b>{current_mode_name}</b>\n"
        f"🤖 Текущая модель LLM: {current_model_name}\n"
        f"📦 Версия бота: <b>{bot_version}</b>\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# ----------------------------
# /accounts — Avito аккаунты (multi-account)
# ----------------------------
def _build_accounts_menu_ui() -> tuple[str, InlineKeyboardMarkup]:
    accounts = list_accounts()
    lines = [
        "👥 <b>Avito аккаунты</b>",
        "",
        "Здесь можно включать/ставить на паузу и выбирать режим для каждого аккаунта.",
    ]
    if not accounts:
        lines += ["", "Аккаунтов пока нет."]
    else:
        lines += ["", f"Всего: <b>{len(accounts)}</b>"]

    buttons: List[List[InlineKeyboardButton]] = []
    for a in accounts[:25]:
        aid = str(a.get("account_id") or "").strip()
        if not aid:
            continue
        name = (a.get("name") or "").strip()
        paused = bool(a.get("paused", False))
        mode = (a.get("mode") or BOT_MODE_FULL).strip()
        partial = int(a.get("partial_percentage", 50) or 50)
        has_creds = bool((a.get("client_id") or "").strip() and (a.get("client_secret") or "").strip())
        status_icon = "⏸" if paused else "▶️"
        mode_icon = {"listening": "🧠", "partial": "🧪", "full": "🚀"}.get(mode, "⚙️")
        creds_icon = "🔑" if has_creds else "⚠️"
        title = f"{aid}" + (f" ({name})" if name else "")
        buttons.append([InlineKeyboardButton(text=f"{status_icon} {mode_icon} {creds_icon} {title}", callback_data=f"acc_open:{aid}")])

    buttons.append([
        InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="acc_add"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="accounts_refresh"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@user_router.message(F.text.regexp(r"^/accounts\b"))
async def cmd_accounts(message: Message, state: FSMContext) -> None:
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    await state.clear()
    text, kb = _build_accounts_menu_ui()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data == "accounts_refresh")
async def callback_accounts_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    text, kb = _build_accounts_menu_ui()
    await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")


def _build_account_details_ui(account_id: str) -> tuple[str, InlineKeyboardMarkup]:
    acc = get_avito_account(account_id) or {}
    aid = str(account_id).strip()
    paused = bool(acc.get("paused", False))
    mode = (acc.get("mode") or BOT_MODE_FULL).strip()
    partial = int(acc.get("partial_percentage", 50) or 50)

    text = _account_status_text({**acc, "account_id": aid})
    pause_btn = "▶️ Снять с паузы" if paused else "⏸ Пауза"

    buttons = [
        [InlineKeyboardButton(text=pause_btn, callback_data=f"acc_toggle_pause:{aid}")],
        [InlineKeyboardButton(text="📊 Статистика аккаунта", callback_data=f"acc_stats:{aid}")],
        [
            InlineKeyboardButton(text="🔗 Webhook (этот аккаунт)", callback_data=f"acc_hook_sub:{aid}"),
            InlineKeyboardButton(text="🧪 Диагностика", callback_data=f"acc_diag:{aid}"),
        ],
        [InlineKeyboardButton(text="⚙️ Режим работы", callback_data=f"acc_mode_menu:{aid}")],
        [
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"acc_delete_confirm:{aid}"),
            InlineKeyboardButton(text="🔙 Назад", callback_data="acc_back"),
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@user_router.callback_query(F.data.startswith("acc_stats:"))
async def callback_account_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    aid = (callback.data or "").split(":", 1)[1].strip()
    s = calculate_account_stats(aid)
    if s.get("error"):
        await callback.message.answer(f"❌ {s['error']}")
        text, kb = _build_account_details_ui(aid)
        await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")
        return

    last_ts = s.get("last_activity_ts") or "—"
    text = (
        f"📊 <b>Статистика аккаунта</b>\n"
        f"Account ID: <b>{aid}</b>\n\n"
        f"• Чатов с ответами: <b>{s.get('total_chats', 0)}</b>\n"
        f"• Ответов бота: <b>{s.get('total_bot_responses', 0)}</b>\n"
        f"• Ответов менеджера: <b>{s.get('total_manager_responses', 0)}</b>\n"
        f"• Передач менеджеру: <b>{s.get('manager_transfers', 0)}</b>\n"
        f"• Доля ответов бота: <b>{s.get('bot_response_rate', 0):.1f}%</b>\n"
        f"• Доля ответов менеджера: <b>{s.get('manager_response_rate', 0):.1f}%</b>\n\n"
        f"• Токенов всего: <b>{s.get('total_tokens', 0)}</b>\n"
        f"• Стоимость LLM: <b>{s.get('total_cost_rub', 0):.2f} ₽</b>\n"
        f"• Сэкономлено (оценка): <b>{s.get('net_savings_rub', 0):.2f} ₽</b>\n\n"
        f"• Последняя активность: <code>{last_ts}</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"acc_open:{aid}")]]
    )
    await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data.startswith("acc_open:"))
async def callback_account_open(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    aid = (callback.data or "").split(":", 1)[1].strip()
    text, kb = _build_account_details_ui(aid)
    await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data == "acc_back")
async def callback_account_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    text, kb = _build_accounts_menu_ui()
    await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data.startswith("acc_toggle_pause:"))
async def callback_account_toggle_pause(callback: CallbackQuery) -> None:
    await callback.answer()
    aid = (callback.data or "").split(":", 1)[1].strip()
    acc = get_avito_account(aid) or {}
    new_paused = not bool(acc.get("paused", False))
    set_avito_account_paused(aid, new_paused)
    text, kb = _build_account_details_ui(aid)
    await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")


def _build_account_mode_menu_ui(account_id: str) -> tuple[str, InlineKeyboardMarkup]:
    acc = get_avito_account(account_id) or {}
    aid = str(account_id).strip()
    current_mode = (acc.get("mode") or BOT_MODE_FULL).strip()
    partial = int(acc.get("partial_percentage", 50) or 50)
    buttons = [
        [InlineKeyboardButton(text=_acc_mode_button_text(BOT_MODE_LISTENING, current_mode, partial), callback_data=f"acc_mode_set:{aid}:{BOT_MODE_LISTENING}")],
        [InlineKeyboardButton(text=_acc_mode_button_text(BOT_MODE_PARTIAL, current_mode, partial), callback_data=f"acc_mode_set:{aid}:{BOT_MODE_PARTIAL}")],
        [InlineKeyboardButton(text=_acc_mode_button_text(BOT_MODE_FULL, current_mode, partial), callback_data=f"acc_mode_set:{aid}:{BOT_MODE_FULL}")],
        [InlineKeyboardButton(text=f"🧪 Изменить % для partial (сейчас {partial}%)", callback_data=f"acc_partial_set:{aid}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"acc_open:{aid}")],
    ]
    text = (
        f"⚙️ <b>Режим аккаунта</b>\n\n"
        f"Аккаунт: <b>{aid}</b>\n"
        f"Текущий режим: <b>{_acc_mode_label(current_mode, partial)}</b>\n\n"
        "Выберите режим:"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@user_router.callback_query(F.data.startswith("acc_mode_menu:"))
async def callback_account_mode_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    aid = (callback.data or "").split(":", 1)[1].strip()
    text, kb = _build_account_mode_menu_ui(aid)
    await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data.startswith("acc_mode_set:"))
async def callback_account_mode_set(callback: CallbackQuery) -> None:
    await callback.answer()
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        return
    aid = parts[1].strip()
    mode = parts[2].strip()
    ok, msg = set_avito_account_mode(aid, mode)
    if not ok:
        await callback.message.answer(f"❌ {msg}")
    text, kb = _build_account_mode_menu_ui(aid)
    await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data.startswith("acc_partial_set:"))
async def callback_account_partial_set(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    aid = (callback.data or "").split(":", 1)[1].strip()
    await state.set_state(AdminStates.waiting_for_avito_account_partial_percentage)
    await state.update_data(avito_account_id=aid)
    await callback.message.answer(
        f"Введите процент (0-100) для partial режима аккаунта <b>{aid}</b>.\n"
        f"Можно отменить: /cancel",
        parse_mode="HTML",
    )


@user_router.message(AdminStates.waiting_for_avito_account_partial_percentage)
async def handle_account_partial_percentage(message: Message, state: FSMContext) -> None:
    if not _check_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    aid = str(data.get("avito_account_id") or "").strip()
    txt = (message.text or "").strip()
    try:
        p = int(re.findall(r"\d+", txt)[0]) if re.findall(r"\d+", txt) else int(txt)
    except Exception:
        await message.answer("❌ Нужен процент числом 0-100.")
        return
    p = max(0, min(100, p))
    set_avito_account_mode(aid, BOT_MODE_PARTIAL, partial_percentage=p)
    await state.clear()
    text, kb = _build_account_mode_menu_ui(aid)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data == "acc_add")
async def callback_account_add(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_avito_account_add_account_id)
    await state.update_data(avito_new_account={})
    await callback.message.answer(
        "Шаг 1/4: отправьте <b>account_id</b> (число).\n"
        "Пример: <code>123456</code>\n"
        "Отмена: /cancel",
        parse_mode="HTML",
    )


@user_router.message(AdminStates.waiting_for_avito_account_add_account_id)
async def handle_account_add_account_id(message: Message, state: FSMContext) -> None:
    if not _check_admin(message.from_user.id):
        await state.clear()
        return
    txt = (message.text or "").strip()
    m = re.match(r"^\s*(\d+)\s*$", txt)
    if not m:
        await message.answer("❌ Не вижу числовой account_id. Пример: <code>123456</code>", parse_mode="HTML")
        return
    aid = m.group(1).strip()
    data = await state.get_data()
    payload = dict(data.get("avito_new_account") or {})
    payload["account_id"] = aid
    await state.update_data(avito_new_account=payload)
    await state.set_state(AdminStates.waiting_for_avito_account_add_client_id)
    await message.answer(
        "Шаг 2/4: отправьте <b>Client ID</b> приложения Avito.\n"
        "Отмена: /cancel",
        parse_mode="HTML",
    )


@user_router.message(AdminStates.waiting_for_avito_account_add_client_id)
async def handle_account_add_client_id(message: Message, state: FSMContext) -> None:
    if not _check_admin(message.from_user.id):
        await state.clear()
        return
    txt = (message.text or "").strip()
    if not txt:
        await message.answer("❌ Client ID не должен быть пустым.")
        return
    # Пробуем скрыть сообщение с кредами (если есть права)
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    payload = dict(data.get("avito_new_account") or {})
    payload["client_id"] = txt
    await state.update_data(avito_new_account=payload)
    await state.set_state(AdminStates.waiting_for_avito_account_add_client_secret)
    await message.answer(
        "Шаг 3/4: отправьте <b>Client Secret</b> приложения Avito.\n"
        "Отмена: /cancel",
        parse_mode="HTML",
    )


@user_router.message(AdminStates.waiting_for_avito_account_add_client_secret)
async def handle_account_add_client_secret(message: Message, state: FSMContext) -> None:
    if not _check_admin(message.from_user.id):
        await state.clear()
        return
    txt = (message.text or "").strip()
    if not txt:
        await message.answer("❌ Client Secret не должен быть пустым.")
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    payload = dict(data.get("avito_new_account") or {})
    payload["client_secret"] = txt
    await state.update_data(avito_new_account=payload)
    await state.set_state(AdminStates.waiting_for_avito_account_add_name)
    await message.answer(
        "Шаг 4/4: отправьте <b>название</b> аккаунта (можно пропустить — отправьте '-')\n"
        "Отмена: /cancel",
        parse_mode="HTML",
    )


@user_router.message(AdminStates.waiting_for_avito_account_add_name)
async def handle_account_add_name(message: Message, state: FSMContext) -> None:
    if not _check_admin(message.from_user.id):
        await state.clear()
        return
    name = (message.text or "").strip()
    if name == "-":
        name = ""
    data = await state.get_data()
    payload = dict(data.get("avito_new_account") or {})
    aid = str(payload.get("account_id") or "").strip()
    cid = str(payload.get("client_id") or "").strip()
    csec = str(payload.get("client_secret") or "").strip()
    ok, msg = upsert_avito_account(aid, name=name or None, paused=True)
    if ok:
        ok2, msg2 = set_avito_account_credentials(aid, cid, csec)
        ok = ok and ok2
        msg = msg + (" " + msg2 if msg2 else "")
    await state.clear()
    await message.answer(("✅ " if ok else "❌ ") + msg)
    text, kb = _build_account_details_ui(aid)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data.startswith("acc_delete_confirm:"))
async def callback_account_delete_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    aid = (callback.data or "").split(":", 1)[1].strip()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"acc_delete:{aid}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"acc_open:{aid}"),
            ]
        ]
    )
    await _safe_edit_text(
        callback.message,
        f"🗑️ Удалить аккаунт <b>{aid}</b>?\n"
        "Это удалит его из списка (история чатов не удаляется).",
        reply_markup=kb,
        parse_mode="HTML",
    )


@user_router.callback_query(F.data.startswith("acc_delete:"))
async def callback_account_delete(callback: CallbackQuery) -> None:
    await callback.answer()
    aid = (callback.data or "").split(":", 1)[1].strip()
    ok, msg = delete_avito_account(aid)
    await callback.message.answer(("✅ " if ok else "❌ ") + msg)
    text, kb = _build_accounts_menu_ui()
    await _safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="HTML")


@user_router.callback_query(F.data.startswith("acc_hook_sub:"))
async def callback_account_hook_subscribe(callback: CallbackQuery) -> None:
    await callback.answer()
    if not WEBHOOK_URL:
        await callback.message.answer("❗️ Не задан PUBLIC_BASE_URL в .env")
        return
    aid = (callback.data or "").split(":", 1)[1].strip()
    cid, csec, err = _get_account_creds(aid)
    if err:
        await callback.message.answer(err)
        return
    ok = subscribe_webhook(WEBHOOK_URL, client_id=cid, client_secret=csec)
    await callback.message.answer(
        ("✅ Webhook подписан для аккаунта. " if ok else "❌ Не удалось подписать webhook. ")
        + f"(client_id={cid}, client_secret={_mask_secret(csec)})"
    )


@user_router.callback_query(F.data.startswith("acc_diag:"))
async def callback_account_diag(callback: CallbackQuery) -> None:
    await callback.answer()
    aid = (callback.data or "").split(":", 1)[1].strip()
    cid, csec, err = _get_account_creds(aid)
    if err:
        await callback.message.answer(err)
        return
    if not WEBHOOK_URL:
        await callback.message.answer("❗️ Не задан PUBLIC_BASE_URL в .env (нужен для webhook).")
    try:
        subs = get_subscriptions(client_id=cid, client_secret=csec)
        import json as _json
        # Не спамим: покажем первые 800 символов
        subs_txt = _json.dumps(subs, ensure_ascii=False, indent=2)[:800]
        await callback.message.answer(
            "🧪 Диагностика аккаунта\n"
            f"account_id: {aid}\n"
            f"client_id: {cid}\n"
            f"client_secret: {_mask_secret(csec)}\n\n"
            f"subscriptions (preview):\n<code>{subs_txt}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await callback.message.answer(
            "🧪 Диагностика аккаунта\n"
            f"account_id: {aid}\n"
            f"client_id: {cid}\n"
            f"client_secret: {_mask_secret(csec)}\n\n"
            f"❌ Ошибка запроса subscriptions: {type(e).__name__}: {e}"
        )


@user_router.callback_query(F.data == "bot_on")
async def callback_bot_on(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Включить бота'."""
    await callback.answer()
    set_bot_enabled(True)
    
    # Обновляем меню с новым статусом
    status_text = "🟢 ВКЛЮЧЕН"
    current_mode = get_bot_mode()
    partial_percent = get_partial_percentage()
    current_mode_name = _mode_label(current_mode, partial_percent)
    
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    bot_version = get_bot_version()
    
    buttons = []
    buttons.append([InlineKeyboardButton(text="🔴 Выключить бота", callback_data="bot_off")])
    buttons.append([InlineKeyboardButton(text="⚙️ Режим работы бота", callback_data="bot_mode_menu")])
    buttons.append([InlineKeyboardButton(text="🤖 Выбрать модель LLM", callback_data="llm_model_menu")])
    buttons.append([
        InlineKeyboardButton(text="🔗 Подключить webhook", callback_data="webhook_subscribe"),
        InlineKeyboardButton(text="🔌 Отключить webhook", callback_data="webhook_unsubscribe"),
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    mode_info = f"📊 Режим: {current_mode_name}"
    
    await callback.message.edit_text(
        f"🤖 Управление ботом\n\n"
        f"📊 Текущий статус бота: {status_text}\n"
        f"⚙️ Режим работы: <b>{current_mode_name}</b>\n"
        f"🤖 Текущая модель LLM: {current_model_name}\n"
        f"📦 Версия бота: <b>{bot_version}</b>\n\n"
        "✅ Бот включен.\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@user_router.callback_query(F.data == "bot_off")
async def callback_bot_off(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Выключить бота'."""
    await callback.answer()
    set_bot_enabled(False)
    
    # Обновляем меню с новым статусом
    status_text = "🔴 ВЫКЛЮЧЕН"
    current_mode = get_bot_mode()
    partial_percent = get_partial_percentage()
    current_mode_name = _mode_label(current_mode, partial_percent)
    
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    bot_version = get_bot_version()
    
    buttons = []
    buttons.append([InlineKeyboardButton(text="🟢 Включить бота", callback_data="bot_on")])
    buttons.append([InlineKeyboardButton(text="⚙️ Режим работы бота", callback_data="bot_mode_menu")])
    buttons.append([InlineKeyboardButton(text="🤖 Выбрать модель LLM", callback_data="llm_model_menu")])
    buttons.append([
        InlineKeyboardButton(text="🔗 Подключить webhook", callback_data="webhook_subscribe"),
        InlineKeyboardButton(text="🔌 Отключить webhook", callback_data="webhook_unsubscribe"),
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    mode_info = f"📊 Режим: {current_mode_name}"
    
    await callback.message.edit_text(
        f"🤖 Управление ботом\n\n"
        f"📊 Текущий статус бота: {status_text}\n"
        f"⚙️ Режим работы: <b>{current_mode_name}</b>\n"
        f"🤖 Текущая модель LLM: {current_model_name}\n"
        f"📦 Версия бота: <b>{bot_version}</b>\n\n"
        "⛔️ Бот выключен. Он не будет отвечать на сообщения из Avito.\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@user_router.callback_query(F.data == "bot_mode_menu")
async def callback_bot_mode_menu(callback: CallbackQuery) -> None:
    """Показывает меню выбора режима работы бота."""
    await callback.answer()
    
    current_mode = get_bot_mode()
    partial_percent = get_partial_percentage()

    text, keyboard = _build_bot_mode_menu_ui(current_mode, partial_percent)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@user_router.callback_query(F.data == "bot_mode_listening")
async def callback_bot_mode_listening(callback: CallbackQuery) -> None:
    """Устанавливает режим LISTENING."""
    await callback.answer()
    set_bot_mode(BOT_MODE_LISTENING)
    await callback_bot_mode_menu(callback)


@user_router.callback_query(F.data == "bot_mode_full")
async def callback_bot_mode_full(callback: CallbackQuery) -> None:
    """Устанавливает режим FULL."""
    await callback.answer()
    set_bot_mode(BOT_MODE_FULL)
    await callback_bot_mode_menu(callback)


@user_router.callback_query(F.data == "bot_mode_partial")
async def callback_bot_mode_partial(callback: CallbackQuery, state: FSMContext) -> None:
    """Устанавливает режим PARTIAL и запрашивает процент."""
    await callback.answer()
    set_bot_mode(BOT_MODE_PARTIAL)
    
    current_percent = get_partial_percentage()

    # Запомним сообщение меню режимов — после ввода процента мы отредактируем его (без необходимости заново вызывать меню)
    if callback.message:
        await state.update_data(
            bot_mode_menu_chat_id=callback.message.chat.id,
            bot_mode_menu_message_id=callback.message.message_id,
        )

    await callback.message.answer(
        "🧪 <b>УЧУСЬ И ЧАСТИЧНО ОТВЕЧАЮ</b>\n\n"
        f"Текущий процент: <b>{current_percent}%</b>\n\n"
        "Введите новый процент (0-100), например: 25\n\n"
        "💡 Для отмены отправьте /cancel",
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_for_partial_percentage)


@user_router.message(AdminStates.waiting_for_partial_percentage, F.text.regexp(r"^/cancel\b"))
async def handle_partial_percentage_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет ввод процента для режима partial."""
    await state.clear()
    await message.answer("❌ Ввод процента отменен.")


@user_router.message(AdminStates.waiting_for_partial_percentage)
async def handle_partial_percentage(message: Message, state: FSMContext) -> None:
    """Обрабатывает ввод процента для режима partial."""
    if not message.text:
        await message.answer("Пожалуйста, введите число от 0 до 100 или /cancel для отмены.")
        return
    
    try:
        percentage = int(message.text.strip())
        
        if percentage < 0 or percentage > 100:
            await message.answer("❌ Процент должен быть от 0 до 100. Попробуйте еще раз или /cancel для отмены.")
            return
        
        set_partial_percentage(percentage)
        await message.answer(f"✅ Процент установлен: <b>{percentage}%</b>", parse_mode="HTML")

        # Обновляем текущее меню режимов (edit_text) — без необходимости заново вызывать меню
        data = await state.get_data()
        chat_id = data.get("bot_mode_menu_chat_id")
        msg_id = data.get("bot_mode_menu_message_id")
        if chat_id and msg_id:
            try:
                current_mode = get_bot_mode()
                partial_percent = get_partial_percentage()
                text, keyboard = _build_bot_mode_menu_ui(current_mode, partial_percent)
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to update bot mode menu message after percentage change: %s", e)

        await state.clear()
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число от 0 до 100 или /cancel для отмены.")


@user_router.callback_query(F.data == "bot_status")
async def callback_bot_status(callback: CallbackQuery) -> None:
    """Обработчик кнопки статуса (когда бот уже в нужном состоянии)."""
    await callback.answer("Бот уже в этом состоянии", show_alert=False)


@user_router.callback_query(F.data == "webhook_subscribe")
async def callback_webhook_subscribe(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Подключить webhook'."""
    await callback.answer()
    
    if not WEBHOOK_URL:
        await callback.message.answer("❗️ Не задан PUBLIC_BASE_URL в .env")
        return
    
    creds = _unique_avito_app_creds()
    if not creds:
        # fallback на .env (старый режим)
        ok = subscribe_webhook(WEBHOOK_URL)
        await callback.message.answer("✅ Вебхук зарегистрирован." if ok else "❌ Ошибка регистрации вебхука.")
        return
    ok_count = 0
    for c in creds:
        ok = subscribe_webhook(WEBHOOK_URL, client_id=c["client_id"], client_secret=c["client_secret"])
        ok_count += 1 if ok else 0
    await callback.message.answer(f"🔗 Webhook: успешно {ok_count}/{len(creds)} приложений.")


@user_router.callback_query(F.data == "webhook_unsubscribe")
async def callback_webhook_unsubscribe(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Отключить webhook'."""
    await callback.answer()
    
    if not WEBHOOK_URL:
        await callback.message.answer("❗️ Не задан PUBLIC_BASE_URL в .env")
        return
    
    creds = _unique_avito_app_creds()
    if not creds:
        ok = unsubscribe_webhook(WEBHOOK_URL)
        await callback.message.answer("✅ Вебхук отключён." if ok else "❌ Ошибка отключения вебхука.")
        return
    ok_count = 0
    for c in creds:
        ok = unsubscribe_webhook(WEBHOOK_URL, client_id=c["client_id"], client_secret=c["client_secret"])
        ok_count += 1 if ok else 0
    await callback.message.answer(f"🔌 Webhook: успешно отключено {ok_count}/{len(creds)} приложений.")


@user_router.callback_query(F.data == "llm_model_menu")
async def callback_llm_model_menu(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Выбрать модель LLM' - показывает меню выбора модели."""
    await callback.answer()
    
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    
    # Создаем кнопки для выбора модели
    buttons = []
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if current_model == 'gpt-5' else ''} Chat GPT 5",
        callback_data="llm_model_gpt5"
    )])
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if current_model == 'gpt-5-mini' else ''} Chat GPT 5 mini",
        callback_data="llm_model_gpt5mini"
    )])
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if current_model == 'gpt-4o' else ''} Chat GPT 4o",
        callback_data="llm_model_gpt4o"
    )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="bot_status_back")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(
        f"🤖 Выбор модели LLM\n\n"
        f"📊 Текущая модель: {current_model_name}\n\n"
        "Выберите модель:",
        reply_markup=keyboard
    )


@user_router.callback_query(F.data == "llm_model_gpt5")
async def callback_llm_model_gpt5(callback: CallbackQuery) -> None:
    """Обработчик выбора модели GPT-5."""
    await callback.answer()
    set_llm_model("gpt-5")
    await _update_llm_model_menu(callback, "gpt-5", "Chat GPT 5")


@user_router.callback_query(F.data == "llm_model_gpt5mini")
async def callback_llm_model_gpt5mini(callback: CallbackQuery) -> None:
    """Обработчик выбора модели GPT-5-mini."""
    await callback.answer()
    set_llm_model("gpt-5-mini")
    await _update_llm_model_menu(callback, "gpt-5-mini", "Chat GPT 5 mini")


@user_router.callback_query(F.data == "llm_model_gpt4o")
async def callback_llm_model_gpt4o(callback: CallbackQuery) -> None:
    """Обработчик выбора модели GPT-4o."""
    await callback.answer()
    set_llm_model("gpt-4o")
    await _update_llm_model_menu(callback, "gpt-4o", "Chat GPT 4o")


async def _update_llm_model_menu(callback: CallbackQuery, model: str, model_name: str) -> None:
    """Обновляет меню выбора модели после выбора."""
    buttons = []
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if model == 'gpt-5' else ''} Chat GPT 5",
        callback_data="llm_model_gpt5"
    )])
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if model == 'gpt-5-mini' else ''} Chat GPT 5 mini",
        callback_data="llm_model_gpt5mini"
    )])
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if model == 'gpt-4o' else ''} Chat GPT 4o",
        callback_data="llm_model_gpt4o"
    )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="bot_status_back")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(
        f"🤖 Выбор модели LLM\n\n"
        f"✅ Модель изменена на: {model_name}\n\n"
        "Выберите модель:",
        reply_markup=keyboard
    )


@user_router.callback_query(F.data == "bot_status_back")
async def callback_bot_status_back(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Назад' - возвращает в главное меню управления ботом."""
    await callback.answer()
    
    current_status = is_bot_enabled()
    status_text = "🟢 ВКЛЮЧЕН" if current_status else "🔴 ВЫКЛЮЧЕН"
    
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    bot_version = get_bot_version()
    
    buttons = []
    if current_status:
        buttons.append([InlineKeyboardButton(text="🔴 Выключить бота", callback_data="bot_off")])
    else:
        buttons.append([InlineKeyboardButton(text="🟢 Включить бота", callback_data="bot_on")])
    buttons.append([InlineKeyboardButton(text="🤖 Выбрать модель LLM", callback_data="llm_model_menu")])
    buttons.append([
        InlineKeyboardButton(text="🔗 Подключить webhook", callback_data="webhook_subscribe"),
        InlineKeyboardButton(text="🔌 Отключить webhook", callback_data="webhook_unsubscribe"),
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(
        f"🤖 Управление ботом\n\n"
        f"📊 Текущий статус бота: {status_text}\n"
        f"🤖 Текущая модель LLM: {current_model_name}\n"
        f"📦 Версия бота: <b>{bot_version}</b>\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# ----------------------------
# /stats — статистика работы бота
# ----------------------------
@user_router.message(F.text.regexp(r"^/stats\b"))
async def cmd_stats(message: Message, state: FSMContext) -> None:
    """Показывает статистику работы бота."""
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    await state.clear()
    
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        stats = calculate_stats()
        
        text = (
            "📊 <b>Статистика работы бота</b>\n\n"
            f"<b>Всего чатов в авито:</b> {stats['total_chats']}\n"
            f"<b>Ответов бота:</b> {stats['total_bot_responses']}"
            f" ({stats['bot_response_rate']:.1f}%)\n"
            f"<b>Ответов менеджера:</b> {stats['total_manager_responses']}"
            f" ({stats['manager_response_rate']:.1f}%)\n"
            f"<b>Переводы на менеджера:</b> {stats['manager_transfers']}"
            f" ({stats['manager_transfer_rate']:.1f}%)\n"
            f"<b>Завершенные ботом:</b> {stats['bot_finished_dialogs']}"
            f" ({stats['bot_finish_rate']:.1f}%)\n"
            f"<b>Завершенные менеджером:</b> {stats['manager_finished_dialogs']}"
            f" ({stats['manager_finish_rate']:.1f}%)\n\n"
            f"📚 <b>База знаний FAQ:</b>\n"
            f"   • Всего вопросов: {stats['faq_total']}\n"
            f"   • Добавлено админом: {stats['faq_admin']}\n"
            f"   • Ответы менеджеров: {stats['faq_manager']}\n"
            f"   • Лайкнуто менеджером: {stats['faq_manager_like']}\n\n"
            f"💰 <b>Использование LLM:</b>\n"
            f"   • Токенов в промптах: {stats['total_prompt_tokens']:,}\n"
            f"   • Токенов в ответах: {stats['total_completion_tokens']:,}\n"
            f"   • Всего токенов: {stats['total_tokens']:,}\n"
            f"   • Стоимость LLM: ${stats['total_cost_usd']:.4f} ({stats['total_cost_rub']:.2f} ₽)\n\n"
            f"⏱️ <b>Время ответа менеджера:</b>\n"
            f"   • Среднее время ответа: {stats['avg_manager_response_time_seconds']:.0f} сек ({stats['avg_manager_response_time_hours']:.2f} ч)\n\n"
            f"💵 <b>Экономика:</b>\n"
            f"   • Сэкономлено времени: {stats['saved_time_hours']:.2f} ч\n"
            f"   • Сэкономлено денег: {stats['saved_money_rub']:.2f} ₽\n"
            f"   • Чистая экономия: {stats['net_savings_rub']:.2f} ₽"
        )
        
        await message.answer(text, parse_mode="HTML")
        logger.info("/stats вызван пользователем %d", message.from_user.id)


# ----------------------------
# /agnt_week_overall — анализ истории чатов за неделю
# ----------------------------
@user_router.message(F.text.regexp(r"^/agnt_week_overall\b"))
async def cmd_agnt_week_overall(message: Message, state: FSMContext) -> None:
    """
    Анализирует историю чатов за последнюю неделю и генерирует инсайты и саммари.
    
    Просматривает все чаты Avito за неделю и выдает:
    - Ключевые вопросы клиентов
    - Как можно улучшить ответы менеджеров и бота
    - Где основные проблемы
    - Почему переписки не приводят к продажам
    - Как можно улучшить продажи
    - Как можно улучшить ответы
    """
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    await state.clear()
    
    # Показываем меню выбора LLM модели
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    
    # Создаем кнопки для выбора модели
    buttons = []
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if current_model == 'gpt-5' else ''} Chat GPT 5",
        callback_data="week_analysis_gpt5"
    )])
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if current_model == 'gpt-5-mini' else ''} Chat GPT 5 mini",
        callback_data="week_analysis_gpt5mini"
    )])
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if current_model == 'gpt-4o' else ''} Chat GPT 4o",
        callback_data="week_analysis_gpt4o"
    )])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(
        f"🤖 <b>Анализ истории чатов за неделю</b>\n\n"
        f"📊 Выберите модель LLM для анализа:\n"
        f"Текущая модель: <b>{current_model_name}</b>\n\n"
        "Выберите модель:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# Обработчики выбора модели для анализа недели
@user_router.callback_query(F.data == "week_analysis_gpt5")
async def callback_week_analysis_gpt5(callback: CallbackQuery) -> None:
    """Обработчик выбора модели GPT-5 для анализа недели."""
    await callback.answer()
    await _run_week_analysis(callback.message, "gpt-5")


@user_router.callback_query(F.data == "week_analysis_gpt5mini")
async def callback_week_analysis_gpt5mini(callback: CallbackQuery) -> None:
    """Обработчик выбора модели GPT-5-mini для анализа недели."""
    await callback.answer()
    await _run_week_analysis(callback.message, "gpt-5-mini")


@user_router.callback_query(F.data == "week_analysis_gpt4o")
async def callback_week_analysis_gpt4o(callback: CallbackQuery) -> None:
    """Обработчик выбора модели GPT-4o для анализа недели."""
    await callback.answer()
    await _run_week_analysis(callback.message, "gpt-4o")


async def _run_week_analysis(message: Message, model: str) -> None:
    """
    Выполняет анализ истории чатов за неделю с указанной моделью LLM.
    
    Args:
        message: Сообщение для отправки результатов
        model: Модель LLM для анализа
    """
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        await message.answer(f"🤖 Анализирую историю чатов за последнюю неделю с моделью {model}... Это может занять некоторое время.")
        
        try:
            # Загружаем историю чатов
            from responder import _load_json, CHAT_HISTORY_PATH
            chat_history = _load_json(CHAT_HISTORY_PATH, {})
            
            # Вычисляем дату неделю назад
            from datetime import datetime, timedelta
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            
            # Собираем все сообщения за неделю из чатов Avito
            week_messages = []
            total_chats = 0
            total_messages = 0
            
            for dialog_id, messages in chat_history.items():
                # Фильтруем только чаты Avito
                if not dialog_id.startswith("avito_"):
                    continue
                
                if not isinstance(messages, list):
                    continue
                
                # Фильтруем сообщения за последнюю неделю
                chat_week_messages = []
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    
                    timestamp_str = msg.get("timestamp")
                    if not timestamp_str:
                        continue
                    
                    try:
                        # Парсим timestamp в разных форматах
                        if 'Z' in timestamp_str:
                            msg_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        elif '+' in timestamp_str or timestamp_str.count('-') > 2:
                            # Формат с timezone
                            msg_time = datetime.fromisoformat(timestamp_str)
                        else:
                            # Формат без timezone
                            msg_time = datetime.fromisoformat(timestamp_str)
                        
                        # Убираем timezone для сравнения
                        if msg_time.tzinfo:
                            msg_time = msg_time.replace(tzinfo=None)
                        
                        if msg_time >= week_ago:
                            chat_week_messages.append(msg)
                            total_messages += 1
                    except (ValueError, TypeError) as e:
                        logger.warning("Ошибка парсинга timestamp %s: %s", timestamp_str, e)
                        continue
                
                if chat_week_messages:
                    total_chats += 1
                    week_messages.append({
                        "dialog_id": dialog_id,
                        "messages": chat_week_messages
                    })
            
            if not week_messages:
                await message.answer(
                    "📊 <b>Анализ истории чатов за неделю</b>\n\n"
                    "❌ Не найдено сообщений за последнюю неделю.\n\n"
                    "Проверьте, что:\n"
                    "• Есть активные чаты Avito\n"
                    "• Сообщения имеют корректные timestamp",
                    parse_mode="HTML"
                )
                return
            
            # Форматируем историю для анализа LLM
            formatted_history = []
            for chat_data in week_messages:
                dialog_id = chat_data["dialog_id"]
                messages = chat_data["messages"]
                
                # Форматируем сообщения чата
                chat_text = f"=== Чат: {dialog_id} ===\n"
                for msg in messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "").strip()
                    timestamp = msg.get("timestamp", "")
                    
                    if not content:
                        continue
                    
                    # Определяем отправителя
                    if role == "user":
                        sender = "👤 Клиент"
                    elif role == "assistant":
                        sender = "🤖 Бот"
                    elif role == "manager":
                        sender = "👨‍💼 Менеджер"
                    else:
                        sender = "❓ Неизвестно"
                    
                    # Форматируем дату
                    date_str = ""
                    if timestamp:
                        try:
                            # Парсим timestamp в разных форматах
                            if 'Z' in timestamp:
                                msg_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            elif '+' in timestamp or timestamp.count('-') > 2:
                                # Формат с timezone
                                msg_time = datetime.fromisoformat(timestamp)
                            else:
                                # Формат без timezone
                                msg_time = datetime.fromisoformat(timestamp)
                            
                            # Убираем timezone для форматирования
                            if msg_time.tzinfo:
                                msg_time = msg_time.replace(tzinfo=None)
                            
                            date_str = msg_time.strftime("%Y-%m-%d %H:%M")
                        except (ValueError, TypeError):
                            pass
                    
                    chat_text += f"[{date_str}] {sender}: {content}\n"
                
                formatted_history.append(chat_text)
            
            # Объединяем всю историю
            full_history_text = "\n\n".join(formatted_history)
            
            # Ограничиваем размер истории для LLM (если слишком большая)
            MAX_HISTORY_LENGTH = 50000  # Ограничение для промпта
            if len(full_history_text) > MAX_HISTORY_LENGTH:
                # Берем последние N символов
                full_history_text = "..." + full_history_text[-MAX_HISTORY_LENGTH:]
                logger.warning("История слишком большая, обрезана до %d символов", MAX_HISTORY_LENGTH)
            
            # Создаем промпт для анализа (без звездочек и решеток в заголовках)
            analysis_prompt = f"""Ты — эксперт по анализу бизнес-коммуникаций и продажам. 

Проанализируй историю чатов с клиентами за последнюю неделю и выдай подробный анализ с инсайтами и рекомендациями.

ИСТОРИЯ ЧАТОВ ЗА НЕДЕЛЮ:
{full_history_text}

СТАТИСТИКА:
- Всего чатов за неделю: {total_chats}
- Всего сообщений за неделю: {total_messages}

ЗАДАЧА:
Проведи глубокий анализ и выдай структурированный отчет со следующими разделами:

1. ОБЩАЯ СТАТИСТИКА И САММАРИ
   - Краткое резюме активности за неделю
   - Количество уникальных клиентов
   - Средняя длина диалогов
   - Распределение ответов (бот/менеджер)

2. КЛЮЧЕВЫЕ ВОПРОСЫ КЛИЕНТОВ
   - Топ-10 самых частых вопросов
   - Категории вопросов (визы, документы, сроки, цены и т.д.)
   - Тренды и паттерны в вопросах

3. ОСНОВНЫЕ ПРОБЛЕМЫ
   - Где возникают проблемы в коммуникации
   - Какие вопросы остаются без ответа
   - Где клиенты теряют интерес
   - Технические проблемы (если есть)

4. АНАЛИЗ ОТВЕТОВ БОТА И МЕНЕДЖЕРОВ
   - Качество ответов бота (что хорошо, что плохо)
   - Качество ответов менеджеров (что хорошо, что плохо)
   - Сравнение эффективности бота и менеджеров
   - Примеры хороших и плохих ответов

5. ПОЧЕМУ ПЕРЕПИСКИ НЕ ПРИВОДЯТ К ПРОДАЖАМ
   - Причины потери клиентов
   - Моменты, где клиенты уходят
   - Что мешает конверсии в продажу
   - Паттерны неуспешных диалогов

6. КАК УЛУЧШИТЬ ПРОДАЖИ
   - Конкретные рекомендации по увеличению конверсии
   - Что нужно добавить в ответы
   - Как лучше работать с возражениями
   - Как ускорить процесс продажи

7. КАК УЛУЧШИТЬ ОТВЕТЫ
   - Рекомендации по улучшению ответов бота
   - Рекомендации по улучшению ответов менеджеров
   - Что добавить в FAQ
   - Какие фразы использовать/избегать

8. ПРИОРИТЕТНЫЕ ДЕЙСТВИЯ
   - Топ-5 самых важных улучшений
   - Что сделать в первую очередь
   - Краткосрочные и долгосрочные цели

ВАЖНО:
- Будь конкретным и практичным
- Приводи примеры из истории
- Давай actionable рекомендации
- Используй эмодзи для структурирования
- Форматируй ответ для удобного чтения в Telegram
- НЕ используй звездочки и решетки в заголовках разделов
- Используй простые заголовки типа "1. ОБЩАЯ СТАТИСТИКА" без специальных символов

Отвечай на русском языке, структурированно и подробно."""

            # Отправляем запрос в LLM
            logger.info("Отправляю запрос в LLM для анализа истории за неделю: %d чатов, %d сообщений, модель: %s", 
                      total_chats, total_messages, model)
            
            use_temperature = model not in ["gpt-5-mini", "gpt-5"]
            
            if use_temperature:
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": analysis_prompt}],
                    temperature=0.7,  # Умеренная температура для креативного анализа
                )
            else:
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": analysis_prompt}],
                )
            
            analysis_result = response.choices[0].message.content.strip()
            
            # Очищаем результат от звездочек и решеток в заголовках
            # Убираем звездочки и решетки из начала строк (заголовки)
            analysis_result = re.sub(r'^[#*]+\s*', '', analysis_result, flags=re.MULTILINE)
            # Убираем звездочки и решетки из середины строк (если есть)
            analysis_result = re.sub(r'\s*[#*]+\s*', ' ', analysis_result)
            
            # Разбиваем результат на части по разделам
            MAX_MESSAGE_LENGTH = 3500  # Безопасное ограничение для Telegram (~4096 символов)
            
            # Отправляем заголовок со статистикой
            header = (
                f"📊 <b>Анализ истории чатов за неделю</b>\n\n"
                f"📈 <b>Статистика:</b>\n"
                f"• Чатов за неделю: {total_chats}\n"
                f"• Сообщений за неделю: {total_messages}\n"
                f"• Модель LLM: {model}\n\n"
                f"{'=' * 50}\n\n"
            )
            await message.answer(header, parse_mode="HTML")
            
            # Разбиваем текст на части по абзацам
            paragraphs = analysis_result.split('\n\n')
            current_part = ""
            part_number = 0
            
            for paragraph in paragraphs:
                paragraph = paragraph.strip()
                if not paragraph:
                    continue
                
                # Проверяем, является ли абзац заголовком раздела (начинается с цифры и точки)
                is_section_header = bool(re.match(r'^\d+\.\s+[А-ЯЁ\s]+', paragraph))
                
                # Если это заголовок раздела, форматируем его
                if is_section_header:
                    # Убираем звездочки и решетки из заголовка
                    paragraph = re.sub(r'^[#*]+\s*', '', paragraph)
                    paragraph = f"<b>{paragraph}</b>"
                
                # Проверяем, поместится ли абзац в текущую часть
                test_length = len(current_part) + len(paragraph) + 4  # +4 для "\n\n"
                
                if test_length > MAX_MESSAGE_LENGTH:
                    # Если текущая часть не пуста, отправляем её
                    if current_part:
                        part_number += 1
                        await message.answer(current_part, parse_mode="HTML")
                        current_part = ""
                    
                    # Если абзац сам по себе слишком длинный, разбиваем его на предложения
                    if len(paragraph) > MAX_MESSAGE_LENGTH:
                        # Разбиваем на предложения
                        sentences = re.split(r'([.!?]\s+)', paragraph)
                        for sentence in sentences:
                            if not sentence.strip():
                                continue
                            
                            if len(current_part) + len(sentence) + 4 > MAX_MESSAGE_LENGTH:
                                if current_part:
                                    part_number += 1
                                    await message.answer(current_part, parse_mode="HTML")
                                current_part = sentence
                            else:
                                current_part += sentence
                    else:
                        current_part = paragraph + "\n\n"
                else:
                    # Добавляем абзац к текущей части
                    if current_part:
                        current_part += "\n\n" + paragraph
                    else:
                        current_part = paragraph
            
            # Отправляем оставшуюся часть
            if current_part:
                await message.answer(current_part, parse_mode="HTML")
            
            logger.info("✅ Анализ истории за неделю завершен пользователем %d", message.from_user.id)
            
        except Exception as e:
            logger.exception("Ошибка при анализе истории за неделю: %s", e)
            await message.answer(
                f"❌ Ошибка при анализе истории чатов за неделю:\n\n{str(e)}\n\n"
                "Проверьте логи для подробностей."
            )


# ----------------------------
# /faq — управление FAQ
# ----------------------------
@user_router.message(F.text.regexp(r"^/knowledge\b|^/kb\b|^/faq\b"))
async def cmd_knowledge_menu(message: Message, state: FSMContext) -> None:
    """
    Показывает меню управления базой знаний.
    
    Поддерживает команды: /knowledge, /kb, /faq (для обратной совместимости)
    
    Args:
        message: Сообщение с командой
        state: FSM контекст для управления состоянием
    """
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    await state.clear()
    
    # Создаем inline кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Добавить знания (текстом)", callback_data="kb_add_text")],
        [InlineKeyboardButton(text="📎 Загрузить файл с переписками", callback_data="kb_upload_dialogues")],
        [
            InlineKeyboardButton(text="📤 Скачать базу знаний", callback_data="kb_download"),
            InlineKeyboardButton(text="👁️ Просмотреть", callback_data="kb_view_all"),
        ],
        [InlineKeyboardButton(text="🛠 Управление карточками", callback_data="kb_manage_menu")],
    ])
    
    await message.answer(
        "🧠 Управление базой знаний\n\n"
        "📌 <b>База знаний</b> хранится в knowledge cards (topic + facts). "
        "Вы можете добавлять информацию в <b>любом формате</b> (как заметки, FAQ, прайс, правила) — "
        "бот сам структурирует это в карточки с помощью LLM.\n\n"
        "💡 <b>Новые возможности:</b>\n"
        "• Загрузка файлов с переписками для автоматического извлечения знаний\n"
        "• Поддержка больших файлов (txt, json, csv, html)\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@user_router.callback_query(F.data == "kb_manage_menu")
async def callback_kb_manage_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню управления knowledge cards."""
    await callback.answer()
    if not _check_admin(callback.from_user.id):
        await callback.message.answer("⛔️ Недостаточно прав.")
        return
    await state.clear()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕒 Последние изменения", callback_data="kb_recent")],
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="kb_search")],
        [InlineKeyboardButton(text="📄 Показать карточку по теме", callback_data="kb_view")],
        [InlineKeyboardButton(text="➕ Добавить факт", callback_data="kb_add_fact")],
        [InlineKeyboardButton(text="📝 Добавить знания из текста", callback_data="kb_add_text")],
        [InlineKeyboardButton(text="🤖 Извлечь знания из переписок", callback_data="kb_extract")],
        [InlineKeyboardButton(text="🧩 Склеить темы", callback_data="kb_merge")],
        [InlineKeyboardButton(text="🗑️ Удалить тему", callback_data="kb_delete")],
    ])
    await callback.message.answer(
        "🛠 Управление knowledge cards — выберите действие:\n\n"
        "💡 <b>Новые возможности:</b>\n"
        "• Добавление знаний из текста (автоматическое извлечение)\n"
        "• Автоматическое извлечение знаний из переписок через LLM",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@user_router.callback_query(F.data == "kb_recent")
async def callback_kb_recent(callback: CallbackQuery) -> None:
    await callback.answer()
    if not _check_admin(callback.from_user.id):
        await callback.message.answer("⛔️ Недостаточно прав.")
        return
    cards = list_recent_cards(limit=10)
    if not cards:
        await callback.message.answer("🧠 База знаний пустая.")
        return
    lines = ["🕒 Последние изменения (top 10):", ""]
    for i, c in enumerate(cards, 1):
        topic = (c.get("topic") or "").strip()
        ts = (c.get("updated_at") or c.get("created_at") or "").strip()
        lines.append(f"{i}. {topic} ({ts})")
    await callback.message.answer("\n".join(lines))


@user_router.callback_query(F.data == "kb_search")
async def callback_kb_search(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("🔎 Введите текст для поиска по темам/фактам (или /cancel):")
    await state.set_state(AdminStates.waiting_for_knowledge_search)


@user_router.message(AdminStates.waiting_for_knowledge_search, F.text.regexp(r"^/cancel\b"))
async def handle_kb_search_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Отменено.")


@user_router.message(AdminStates.waiting_for_knowledge_search)
async def handle_kb_search(message: Message, state: FSMContext) -> None:
    q = (message.text or "").strip()
    if not q:
        await message.answer("Введите текст для поиска или /cancel.")
        return
    # Используем улучшенный поиск
    try:
        results = search_knowledge_cards(q, limit=10, min_relevance=0.3)
        if not results:
            await state.clear()
            await message.answer("Ничего не найдено.")
            return
        lines = [f"🔎 Найдено (top {len(results)}):", ""]
        for i, (score, c) in enumerate(results, 1):
            topic = (c.get("topic") or "").strip()
            category = c.get("category", "")
            lines.append(f"{i}. {topic} (релевантность: {score:.2f}, категория: {category})")
        await state.clear()
        await message.answer("\n".join(lines))
    except Exception as e:
        logger.exception("Ошибка при поиске: %s", e)
        # Fallback на старый метод
        res = find_cards(q, limit=10)
        if not res:
            await state.clear()
            await message.answer("Ничего не найдено.")
            return
        lines = [f"🔎 Найдено (top {len(res)}):", ""]
        for i, c in enumerate(res, 1):
            topic = (c.get("topic") or "").strip()
            lines.append(f"{i}. {topic}")
        await state.clear()
        await message.answer("\n".join(lines))


@user_router.callback_query(F.data == "kb_view")
async def callback_kb_view(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("📄 Введите topic (точное название темы) или /cancel:")
    await state.set_state(AdminStates.waiting_for_knowledge_view)


@user_router.message(AdminStates.waiting_for_knowledge_view, F.text.regexp(r"^/cancel\b"))
async def handle_kb_view_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Отменено.")


@user_router.message(AdminStates.waiting_for_knowledge_view)
async def handle_kb_view(message: Message, state: FSMContext) -> None:
    topic = (message.text or "").strip()
    if not topic:
        await message.answer("Введите topic или /cancel.")
        return
    card = None
    # лёгкий поиск по exact topic среди найденных
    for c in load_knowledge_cards():
        if (c.get("topic") or "").strip().lower() == topic.lower():
            card = c
            break
    if not card:
        await state.clear()
        await message.answer("Тема не найдена. Используйте поиск.")
        return
    facts = card.get("facts") or []
    lines = [f"🧠 Тема: {card.get('topic','')}", ""]
    if isinstance(facts, list) and facts:
        for f in facts[:20]:
            lines.append(f"- {str(f).strip()}")
    else:
        lines.append("(нет фактов)")
    await state.clear()
    await message.answer("\n".join(lines))


@user_router.callback_query(F.data == "kb_delete")
async def callback_kb_delete(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("🗑️ Введите topic для удаления (точно) или /cancel:")
    await state.set_state(AdminStates.waiting_for_knowledge_delete)


@user_router.message(AdminStates.waiting_for_knowledge_delete, F.text.regexp(r"^/cancel\b"))
async def handle_kb_delete_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Отменено.")


@user_router.message(AdminStates.waiting_for_knowledge_delete)
async def handle_kb_delete(message: Message, state: FSMContext) -> None:
    topic = (message.text or "").strip()
    ok, msg = delete_card(topic)
    await state.clear()
    await message.answer(("✅ " if ok else "❌ ") + msg)


@user_router.callback_query(F.data == "kb_add_fact")
async def callback_kb_add_fact(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("➕ Введите topic (тема) куда добавить факт (или /cancel):")
    await state.set_state(AdminStates.waiting_for_knowledge_add_fact_topic)


@user_router.message(AdminStates.waiting_for_knowledge_add_fact_topic, F.text.regexp(r"^/cancel\b"))
async def handle_kb_add_fact_topic_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Отменено.")


@user_router.message(AdminStates.waiting_for_knowledge_add_fact_topic)
async def handle_kb_add_fact_topic(message: Message, state: FSMContext) -> None:
    topic = (message.text or "").strip()
    if not topic:
        await message.answer("Введите topic или /cancel.")
        return
    await state.update_data(kb_fact_topic=topic)
    await message.answer("Теперь отправьте факт (одна строка) или /cancel:")
    await state.set_state(AdminStates.waiting_for_knowledge_add_fact_text)


@user_router.message(AdminStates.waiting_for_knowledge_add_fact_text, F.text.regexp(r"^/cancel\b"))
async def handle_kb_add_fact_text_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Отменено.")


@user_router.message(AdminStates.waiting_for_knowledge_add_fact_text)
async def handle_kb_add_fact_text(message: Message, state: FSMContext) -> None:
    fact = (message.text or "").strip()
    if not fact:
        await message.answer("Факт не может быть пустым. Отправьте текст или /cancel.")
        return
    data = await state.get_data()
    topic = (data.get("kb_fact_topic") or "").strip()
    ok, msg = add_facts(topic, [fact], source="admin_edit", dialog_id=f"tg_admin_{message.from_user.id}")
    await state.clear()
    await message.answer(("✅ " if ok else "❌ ") + msg)


@user_router.callback_query(F.data == "kb_merge")
async def callback_kb_merge(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("🧩 Введите topic-источник (что склеиваем) или /cancel:")
    await state.set_state(AdminStates.waiting_for_knowledge_merge_from)


@user_router.message(AdminStates.waiting_for_knowledge_merge_from, F.text.regexp(r"^/cancel\b"))
async def handle_kb_merge_from_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Отменено.")


@user_router.message(AdminStates.waiting_for_knowledge_merge_from)
async def handle_kb_merge_from(message: Message, state: FSMContext) -> None:
    topic_from = (message.text or "").strip()
    if not topic_from:
        await message.answer("Введите topic или /cancel.")
        return
    await state.update_data(kb_merge_from=topic_from)
    await message.answer("Теперь введите topic-назначение (куда склеить) или /cancel:")
    await state.set_state(AdminStates.waiting_for_knowledge_merge_into)


@user_router.message(AdminStates.waiting_for_knowledge_merge_into, F.text.regexp(r"^/cancel\b"))
async def handle_kb_merge_into_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Отменено.")


@user_router.message(AdminStates.waiting_for_knowledge_merge_into)
async def handle_kb_merge_into(message: Message, state: FSMContext) -> None:
    topic_into = (message.text or "").strip()
    if not topic_into:
        await message.answer("Введите topic или /cancel.")
        return
    data = await state.get_data()
    topic_from = (data.get("kb_merge_from") or "").strip()
    ok, msg = merge_topics(topic_from, topic_into, dialog_id=f"tg_admin_{message.from_user.id}", source="admin_merge")
    await state.clear()
    await message.answer(("✅ " if ok else "❌ ") + msg)


# ----------------------------
# Добавление знаний из текста
# ----------------------------
@user_router.callback_query(F.data == "kb_add_text")
async def callback_kb_add_text(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Добавить знания из текста'."""
    await callback.answer()
    await state.clear()
    await state.update_data(accumulated_text="")
    await callback.message.answer(
        "📝 Отправьте текст для добавления в базу знаний.\n\n"
        "💡 <b>Бот автоматически извлечет знания из текста:</b>\n"
        "• Определит темы\n"
        "• Извлечет факты\n"
        "• Добавит категории и теги\n\n"
        "📌 <b>Можно отправлять частями</b> — каждое сообщение будет добавлено к предыдущему.\n"
        "💡 Для завершения ввода отправьте /done\n"
        "💡 Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_knowledge_add_text)


@user_router.message(AdminStates.waiting_for_knowledge_add_text, F.text.regexp(r"^/cancel\b"))
async def handle_kb_add_text_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет добавление знаний из текста."""
    await state.clear()
    await message.answer("✅ Добавление знаний отменено.")


@user_router.message(AdminStates.waiting_for_knowledge_add_text, F.text.regexp(r"^/done\b"))
async def handle_kb_add_text_done(message: Message, state: FSMContext) -> None:
    """Завершает ввод текста и извлекает знания."""
    data = await state.get_data()
    accumulated_text = data.get("accumulated_text", "").strip()
    
    if not accumulated_text:
        await message.answer("❌ Текст пуст. Отправьте текст или /cancel для отмены.")
        return
    
    try:
        from utils.knowledge_cards import add_knowledge_from_text
        
        count, topics = add_knowledge_from_text(
            accumulated_text,
            source="admin_manual",
            dialog_id=f"tg_admin_{message.from_user.id}"
        )
        
        if count > 0:
            topics_str = "\n".join([f"• {t}" for t in topics[:10]])
            if len(topics) > 10:
                topics_str += f"\n... и еще {len(topics) - 10}"
            await message.answer(
                f"✅ Извлечено знаний: <b>{count}</b> карточек\n\n"
                f"📋 Темы:\n{topics_str}",
                parse_mode="HTML"
            )
        else:
            await message.answer("⚠️ Не удалось извлечь знания из текста. Попробуйте структурировать текст с заголовками и списками.")
    except Exception as e:
        logger.exception("Ошибка при добавлении знаний из текста: %s", e)
        await message.answer(f"❌ Ошибка при обработке: {e}")
    finally:
        await state.clear()


@user_router.message(AdminStates.waiting_for_knowledge_add_text)
async def handle_kb_add_text(message: Message, state: FSMContext) -> None:
    """Обрабатывает текст для добавления знаний (поддерживает ввод частями)."""
    if message.text and message.text.startswith("/"):
        await state.clear()
        logger.info("Команда %s отменила ожидание текста знаний", message.text.split()[0])
        return
    
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение, /done для завершения или /cancel для отмены.")
        return
    
    try:
        new_text = message.text.strip()
        if not new_text:
            await message.answer("❌ Текст не может быть пустым. Отправьте текст, /done для завершения или /cancel для отмены.")
            return
        
        data = await state.get_data()
        accumulated_text = data.get("accumulated_text", "")
        
        if accumulated_text:
            accumulated_text += "\n\n" + new_text
        else:
            accumulated_text = new_text
        
        await state.update_data(accumulated_text=accumulated_text)
        
        await message.answer(
            f"✅ Текст добавлен. Текущая длина: {len(accumulated_text)} символов.\n\n"
            f"💡 Отправьте следующую часть или /done для завершения ввода."
        )
    except Exception as e:
        logger.exception("Ошибка при обработке текста знаний: %s", e)
        await message.answer(f"❌ Ошибка при обработке: {e}")


# ----------------------------
# Автоматическое извлечение знаний из переписок
# ----------------------------
@user_router.callback_query(F.data == "kb_extract")
async def callback_kb_extract(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Извлечь знания из переписок'."""
    await callback.answer()
    if not _check_admin(callback.from_user.id):
        await callback.message.answer("⛔️ Недостаточно прав.")
        return
    
    try:
        from utils.knowledge_extractor import process_dialogs_for_knowledge_extraction
        
        await callback.message.answer("🔄 Начинаю извлечение знаний из переписок... Это может занять некоторое время.")
        
        # Запускаем извлечение асинхронно
        stats = await process_dialogs_for_knowledge_extraction(max_dialogs=50)
        
        await callback.message.answer(
            f"✅ Извлечение знаний завершено!\n\n"
            f"📊 Статистика:\n"
            f"• Обработано диалогов: {stats['processed']}\n"
            f"• Извлечено карточек: {stats['extracted']}\n"
            f"• Ошибок: {stats['errors']}"
        )
    except Exception as e:
        logger.exception("Ошибка при извлечении знаний: %s", e)
        await callback.message.answer(f"❌ Ошибка при извлечении знаний: {e}")


@user_router.callback_query(F.data == "kb_add_text")
async def callback_kb_add_text_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Добавить знания из текста' (из главного меню)."""
    await callback.answer()
    await state.clear()
    await state.update_data(accumulated_text="")
    await callback.message.answer(
        "📝 Отправьте текст для добавления в базу знаний.\n\n"
        "💡 <b>Бот автоматически извлечет знания из текста:</b>\n"
        "• Определит темы\n"
        "• Извлечет факты\n"
        "• Добавит категории и теги\n\n"
        "📌 <b>Можно отправлять частями</b> — каждое сообщение будет добавлено к предыдущему.\n"
        "💡 Для завершения ввода отправьте /done\n"
        "💡 Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_knowledge_add_text)


@user_router.callback_query(F.data == "kb_upload_dialogues")
async def callback_kb_upload_dialogues(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Загрузить файл с переписками'."""
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "📎 Отправьте файл с переписками для извлечения знаний.\n\n"
        "💡 <b>Поддерживаемые форматы:</b>\n"
        "• <b>JSON</b> - массив диалогов или объектов с сообщениями\n"
        "• <b>TXT</b> - текстовые переписки (каждая строка - сообщение)\n"
        "• <b>CSV</b> - таблица с колонками (сообщение, роль, дата и т.д.)\n"
        "• <b>HTML</b> - HTML файлы с переписками\n\n"
        "🤖 Бот автоматически извлечет знания из переписок через LLM.\n"
        "💡 Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_knowledge_file)


@user_router.callback_query(F.data == "kb_download")
async def callback_kb_download(callback: CallbackQuery) -> None:
    """Скачивает базу знаний (knowledge cards)."""
    await callback.answer()
    try:
        if os.path.exists(KNOWLEDGE_CARDS_PATH):
            document = FSInputFile(KNOWLEDGE_CARDS_PATH, filename="knowledge_cards.json")
            await callback.message.answer_document(
                document=document,
                caption="📥 Файл базы знаний (knowledge cards)"
            )
        else:
            await callback.message.answer("❌ Файл knowledge_cards.json не найден.")
    except Exception as e:
        logger.exception("Ошибка при скачивании базы знаний: %s", e)
        await callback.message.answer(f"❌ Ошибка при скачивании базы знаний: {e}")


@user_router.callback_query(F.data == "kb_view_all")
async def callback_kb_view_all(callback: CallbackQuery) -> None:
    """Просмотр базы знаний (превью всех карточек)."""
    await callback.answer()
    try:
        cards = load_knowledge_cards()
        if cards:
            preview = f"🧠 База знаний содержит <b>{len(cards)}</b> тем:\n\n"
            for i, item in enumerate(cards[:5], 1):
                topic = (item.get("topic") or "")[:80]
                facts = item.get("facts") or []
                category = item.get("category", "общее")
                fact0 = ""
                if isinstance(facts, list) and facts:
                    fact0 = str(facts[0])[:80]
                preview += f"{i}. <b>{topic}</b> ({category})\n   - {fact0}...\n\n"
            if len(cards) > 5:
                preview += f"... и еще {len(cards) - 5} тем"
            await callback.message.answer(preview, parse_mode="HTML")
        else:
            await callback.message.answer("🧠 База знаний пустая.")
    except Exception as e:
        logger.exception("Ошибка при просмотре базы знаний: %s", e)
        await callback.message.answer(f"❌ Ошибка при просмотре базы знаний: {e}")


# ----------------------------
# /staticcontext — админ (заменила /setcontext)
# ----------------------------
@user_router.message(F.text.regexp(r"^/staticcontext\b"))
async def cmd_static_context_menu(message: Message, state: FSMContext) -> None:
    """Показывает меню управления статическим контекстом."""
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    await state.clear()
    
    # Создаем inline кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👁️ Просмотреть", callback_data="static_view"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="static_edit"),
        ],
    ])
    
    await message.answer(
        "📋 Управление статическим контекстом\n\n"
        "📌 <b>Статический контекст</b> — то, что не меняется:\n"
        "• Описание компании\n"
        "• Города и адреса посольств и визовых центров\n"
        "• Другая стабильная информация\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )


# Обработчик старой команды /setcontext для обратной совместимости
@user_router.message(F.text.regexp(r"^/setcontext\b"))
async def cmd_setcontext_legacy(message: Message, state: FSMContext) -> None:
    """Обработчик старой команды /setcontext - перенаправляет на /staticcontext."""
    await state.clear()
    # Просто вызываем новую команду
    await cmd_static_context_menu(message, state)


@user_router.callback_query(F.data == "static_view")
async def callback_static_view(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Просмотреть статический контекст'."""
    await callback.answer()
    try:
        if os.path.exists(STATIC_CONTEXT_PATH):
            with open(STATIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            if content:
                # Выводим полностью контент
                # Telegram ограничение на длину сообщения ~4096 символов
                # Если контент длиннее, разбиваем на части (3500 символов с учетом заголовков)
                if len(content) <= 3500:
                    await callback.message.answer(f"📋 Текущий статический контекст:\n\n{content}")
                else:
                    # Разбиваем на части
                    parts = [content[i:i+3500] for i in range(0, len(content), 3500)]
                    for i, part in enumerate(parts, 1):
                        if i == 1:
                            await callback.message.answer(f"📋 Текущий статический контекст (часть {i}/{len(parts)}):\n\n{part}")
                        else:
                            await callback.message.answer(f"📋 Продолжение статического контекста (часть {i}/{len(parts)}):\n\n{part}")
            else:
                await callback.message.answer("📋 Статический контекст пуст.")
        else:
            await callback.message.answer("📋 Статический контекст не установлен.")
    except Exception as e:
        logger.exception("Ошибка при просмотре статического контекста: %s", e)
        await callback.message.answer(f"❌ Ошибка при просмотре: {e}")


@user_router.callback_query(F.data == "static_edit")
async def callback_static_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Изменить статический контекст'."""
    await callback.answer()
    await state.clear()
    await state.update_data(accumulated_text="")
    await callback.message.answer(
        "📝 Отправьте новый текст статичного контекста (он перезапишет старый).\n\n"
        "📌 <b>Статический контекст</b> — то, что не меняется:\n"
        "• Описание компании\n"
        "• Города и адреса посольств и визовых центров\n"
        "• Другая стабильная информация\n\n"
        "💡 <b>Можно отправлять частями</b> — каждое сообщение будет добавлено к предыдущему.\n"
        "💡 Для завершения ввода отправьте /done\n"
        "💡 Для отмены отправьте /cancel"
    )
    await state.set_state(AdminStates.waiting_for_static_context)


# Обработчик для отмены при ожидании контекста
@user_router.message(AdminStates.waiting_for_static_context, F.text.regexp(r"^/cancel\b"))
async def handle_context_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет установку контекста."""
    await state.clear()
    await message.answer("✅ Установка контекста отменена.")


# Обработчик для завершения ввода контекста
@user_router.message(AdminStates.waiting_for_static_context, F.text.regexp(r"^/done\b"))
async def handle_static_context_done(message: Message, state: FSMContext) -> None:
    """Завершает ввод статического контекста и сохраняет его."""
    data = await state.get_data()
    accumulated_text = data.get("accumulated_text", "").strip()
    
    if not accumulated_text:
        await message.answer("❌ Контекст пуст. Отправьте текст или /cancel для отмены.")
        return
    
    try:
        # Сохраняем контекст
        with open(STATIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
            f.write(accumulated_text)
        
        logger.info("✅ Статичный контекст успешно обновлен пользователем %d, длина: %d символов", 
                   message.from_user.id, len(accumulated_text))
        await message.answer(f"✅ Контекст обновлён. Длина: {len(accumulated_text)} символов.")
    except Exception as e:
        logger.exception("Ошибка при сохранении контекста: %s", e)
        await message.answer(f"❌ Ошибка при сохранении контекста: {e}")
    finally:
        await state.clear()


@user_router.message(AdminStates.waiting_for_static_context)
async def handle_static_context(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает текст статического контекста от администратора (поддерживает ввод частями).
    
    Args:
        message: Сообщение с текстом контекста
        state: FSM контекст для управления состоянием
    """
    # Проверяем, не является ли сообщение командой
    if message.text and message.text.startswith("/"):
        # Если это команда, очищаем состояние и не обрабатываем как контекст
        await state.clear()
        logger.info("Команда %s отменила ожидание контекста", message.text.split()[0])
        return
    
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение с контекстом, /done для завершения или /cancel для отмены.")
        return
    
    try:
        new_text = message.text.strip()
        
        # Проверяем, что текст не пустой
        if not new_text:
            await message.answer("❌ Текст не может быть пустым. Отправьте текст, /done для завершения или /cancel для отмены.")
            return
        
        # Получаем накопленный текст
        data = await state.get_data()
        accumulated_text = data.get("accumulated_text", "")
        
        # Добавляем новый текст к накопленному (с переносом строки между частями)
        if accumulated_text:
            accumulated_text += "\n" + new_text
        else:
            accumulated_text = new_text
        
        # Сохраняем накопленный текст в FSM
        await state.update_data(accumulated_text=accumulated_text)
        
        # Показываем текущую длину
        await message.answer(
            f"✅ Текст добавлен. Текущая длина: {len(accumulated_text)} символов.\n\n"
            f"💡 Отправьте следующую часть или /done для завершения ввода."
        )
    except Exception as e:
        logger.exception("Ошибка при обработке контекста: %s", e)
        await message.answer(f"❌ Ошибка при обработке: {e}")


# ----------------------------
# /dynamiccontext — управление динамическим контекстом
# ----------------------------
@user_router.message(F.text.regexp(r"^/dynamiccontext\b"))
async def cmd_dynamic_context_menu(message: Message, state: FSMContext) -> None:
    """Показывает меню управления динамическим контекстом."""
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    await state.clear()
    
    # Создаем inline кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👁️ Просмотреть", callback_data="dynamic_view"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="dynamic_edit"),
        ],
    ])
    
    await message.answer(
        "📊 Управление динамическим контекстом\n\n"
        "📌 <b>Динамический контекст</b> — актуальная информация, которая меняется регулярно:\n"
        "• Цены и тарифы\n"
        "• Сроки подачи документов\n"
        "• Доступность записей\n"
        "• Процент одобрения виз\n"
        "• Условия оплаты и возврата\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@user_router.callback_query(F.data == "dynamic_view")
async def callback_dynamic_view(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Просмотреть динамический контекст'."""
    await callback.answer()
    try:
        if os.path.exists(DYNAMIC_CONTEXT_PATH):
            with open(DYNAMIC_CONTEXT_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            if content:
                # Выводим полностью контент
                # Telegram ограничение на длину сообщения ~4096 символов
                # Если контент длиннее, разбиваем на части (3500 символов с учетом заголовков)
                if len(content) <= 3500:
                    await callback.message.answer(f"📊 Текущий динамический контекст:\n\n{content}")
                else:
                    # Разбиваем на части
                    parts = [content[i:i+3500] for i in range(0, len(content), 3500)]
                    for i, part in enumerate(parts, 1):
                        if i == 1:
                            await callback.message.answer(f"📊 Текущий динамический контекст (часть {i}/{len(parts)}):\n\n{part}")
                        else:
                            await callback.message.answer(f"📊 Продолжение динамического контекста (часть {i}/{len(parts)}):\n\n{part}")
            else:
                await callback.message.answer("📊 Динамический контекст пуст.")
        else:
            await callback.message.answer("📊 Динамический контекст не установлен.")
    except Exception as e:
        logger.exception("Ошибка при просмотре динамического контекста: %s", e)
        await callback.message.answer(f"❌ Ошибка при просмотре: {e}")


@user_router.callback_query(F.data == "dynamic_edit")
async def callback_dynamic_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Изменить динамический контекст'."""
    await callback.answer()
    await state.clear()
    await state.update_data(accumulated_text="")
    await callback.message.answer(
        "📝 Отправьте новый текст динамического контекста (он перезапишет старый).\n\n"
        "📌 <b>Динамический контекст</b> — актуальная информация, которая меняется регулярно:\n"
        "• Цены и тарифы\n"
        "• Сроки подачи документов\n"
        "• Доступность записей\n"
        "• Процент одобрения виз\n"
        "• Условия оплаты и возврата\n\n"
        "💡 <b>Можно отправлять частями</b> — каждое сообщение будет добавлено к предыдущему.\n"
        "💡 Для завершения ввода отправьте /done\n"
        "💡 Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_dynamic_context)


@user_router.message(AdminStates.waiting_for_dynamic_context, F.text.regexp(r"^/cancel\b"))
async def handle_dynamic_context_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет установку динамического контекста."""
    await state.clear()
    await message.answer("✅ Установка динамического контекста отменена.")


@user_router.message(AdminStates.waiting_for_dynamic_context, F.text.regexp(r"^/done\b"))
async def handle_dynamic_context_done(message: Message, state: FSMContext) -> None:
    """Завершает ввод динамического контекста и сохраняет его."""
    data = await state.get_data()
    accumulated_text = data.get("accumulated_text", "").strip()
    
    if not accumulated_text:
        await message.answer("❌ Контекст пуст. Отправьте текст или /cancel для отмены.")
        return
    
    try:
        # Сохраняем контекст
        with open(DYNAMIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
            f.write(accumulated_text)
        
        logger.info("✅ Динамический контекст успешно обновлен пользователем %d, длина: %d символов", 
                   message.from_user.id, len(accumulated_text))
        await message.answer(f"✅ Динамический контекст обновлён. Длина: {len(accumulated_text)} символов.")
    except Exception as e:
        logger.exception("Ошибка при сохранении динамического контекста: %s", e)
        await message.answer(f"❌ Ошибка при сохранении динамического контекста: {e}")
    finally:
        await state.clear()


@user_router.message(AdminStates.waiting_for_dynamic_context)
async def handle_dynamic_context(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает текст динамического контекста от администратора (поддерживает ввод частями).
    
    Args:
        message: Сообщение с текстом контекста
        state: FSM контекст для управления состоянием
    """
    # Проверяем, не является ли сообщение командой
    if message.text and message.text.startswith("/"):
        # Если это команда, очищаем состояние и не обрабатываем как контекст
        await state.clear()
        logger.info("Команда %s отменила ожидание динамического контекста", message.text.split()[0])
        return
    
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение с контекстом, /done для завершения или /cancel для отмены.")
        return
    
    try:
        new_text = message.text.strip()
        
        # Проверяем, что текст не пустой
        if not new_text:
            await message.answer("❌ Текст не может быть пустым. Отправьте текст, /done для завершения или /cancel для отмены.")
            return
        
        # Получаем накопленный текст
        data = await state.get_data()
        accumulated_text = data.get("accumulated_text", "")
        
        # Добавляем новый текст к накопленному (с переносом строки между частями)
        if accumulated_text:
            accumulated_text += "\n" + new_text
        else:
            accumulated_text = new_text
        
        # Сохраняем накопленный текст в FSM
        await state.update_data(accumulated_text=accumulated_text)
        
        # Показываем текущую длину
        await message.answer(
            f"✅ Текст добавлен. Текущая длина: {len(accumulated_text)} символов.\n\n"
            f"💡 Отправьте следующую часть или /done для завершения ввода."
        )
    except Exception as e:
        logger.exception("Ошибка при обработке динамического контекста: %s", e)
        await message.answer(f"❌ Ошибка при обработке: {e}")


# ----------------------------
# /systemprompt — управление системным промптом
# ----------------------------
@user_router.message(F.text.regexp(r"^/systemprompt\b"))
async def cmd_system_prompt_menu(message: Message, state: FSMContext) -> None:
    """Показывает меню управления системным промптом."""
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    await state.clear()
    
    # Создаем inline кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👁️ Просмотреть", callback_data="system_prompt_view"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="system_prompt_edit"),
        ],
    ])
    
    await message.answer(
        "🤖 Управление системным промптом\n\n"
        "📌 <b>Системный промпт</b> — определяет манеру поведения и стиль общения бота:\n"
        "• Характеристики личности бота\n"
        "• Стиль общения\n"
        "• Правила взаимодействия с клиентами\n"
        "• Манера поведения\n\n"
        "⚠️ <b>Не содержит</b> фактов о компании, ценах или услугах.\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@user_router.callback_query(F.data == "system_prompt_view")
async def callback_system_prompt_view(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Просмотреть системный промпт'."""
    await callback.answer()
    try:
        if os.path.exists(SYSTEM_PROMPT_PATH):
            with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            if content:
                # Выводим полностью контент
                # Telegram ограничение на длину сообщения ~4096 символов
                # Если контент длиннее, разбиваем на части (3500 символов с учетом заголовков)
                if len(content) <= 3500:
                    await callback.message.answer(f"🤖 Текущий системный промпт:\n\n{content}")
                else:
                    # Разбиваем на части
                    parts = [content[i:i+3500] for i in range(0, len(content), 3500)]
                    for i, part in enumerate(parts, 1):
                        if i == 1:
                            await callback.message.answer(f"🤖 Текущий системный промпт (часть {i}/{len(parts)}):\n\n{part}")
                        else:
                            await callback.message.answer(f"🤖 Продолжение системного промпта (часть {i}/{len(parts)}):\n\n{part}")
            else:
                await callback.message.answer("🤖 Системный промпт пуст.")
        else:
            await callback.message.answer("🤖 Системный промпт не установлен.")
    except Exception as e:
        logger.exception("Ошибка при просмотре системного промпта: %s", e)
        await callback.message.answer(f"❌ Ошибка при просмотре: {e}")


@user_router.callback_query(F.data == "system_prompt_edit")
async def callback_system_prompt_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Изменить системный промпт'."""
    await callback.answer()
    await state.clear()
    await state.update_data(accumulated_text="")
    await callback.message.answer(
        "📝 Отправьте новый текст системного промпта (он перезапишет старый).\n\n"
        "📌 <b>Системный промпт</b> — определяет манеру поведения и стиль общения бота:\n"
        "• Характеристики личности бота\n"
        "• Стиль общения\n"
        "• Правила взаимодействия с клиентами\n"
        "• Манера поведения\n\n"
        "⚠️ <b>Не содержит</b> фактов о компании, ценах или услугах.\n\n"
        "💡 <b>Можно отправлять частями</b> — каждое сообщение будет добавлено к предыдущему.\n"
        "💡 Для завершения ввода отправьте /done\n"
        "💡 Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_system_prompt)


@user_router.message(AdminStates.waiting_for_system_prompt, F.text.regexp(r"^/cancel\b"))
async def handle_system_prompt_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет установку системного промпта."""
    await state.clear()
    await message.answer("✅ Установка системного промпта отменена.")


@user_router.message(AdminStates.waiting_for_system_prompt, F.text.regexp(r"^/done\b"))
async def handle_system_prompt_done(message: Message, state: FSMContext) -> None:
    """Завершает ввод системного промпта и сохраняет его."""
    data = await state.get_data()
    accumulated_text = data.get("accumulated_text", "").strip()
    
    if not accumulated_text:
        await message.answer("❌ Промпт пуст. Отправьте текст или /cancel для отмены.")
        return
    
    try:
        # Сохраняем промпт
        with open(SYSTEM_PROMPT_PATH, "w", encoding="utf-8") as f:
            f.write(accumulated_text)
        
        logger.info("✅ Системный промпт успешно обновлен пользователем %d, длина: %d символов", 
                   message.from_user.id, len(accumulated_text))
        await message.answer(f"✅ Системный промпт обновлён. Длина: {len(accumulated_text)} символов.")
    except Exception as e:
        logger.exception("Ошибка при сохранении системного промпта: %s", e)
        await message.answer(f"❌ Ошибка при сохранении системного промпта: {e}")
    finally:
        await state.clear()


@user_router.message(AdminStates.waiting_for_system_prompt)
async def handle_system_prompt(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает текст системного промпта от администратора (поддерживает ввод частями).
    
    Args:
        message: Сообщение с текстом промпта
        state: FSM контекст для управления состоянием
    """
    # Проверяем, не является ли сообщение командой
    if message.text and message.text.startswith("/"):
        # Если это команда, очищаем состояние и не обрабатываем как промпт
        await state.clear()
        logger.info("Команда %s отменила ожидание системного промпта", message.text.split()[0])
        return
    
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение с промптом, /done для завершения или /cancel для отмены.")
        return
    
    try:
        new_text = message.text.strip()
        
        # Проверяем, что текст не пустой
        if not new_text:
            await message.answer("❌ Текст не может быть пустым. Отправьте текст, /done для завершения или /cancel для отмены.")
            return
        
        # Получаем накопленный текст
        data = await state.get_data()
        accumulated_text = data.get("accumulated_text", "")
        
        # Добавляем новый текст к накопленному (с переносом строки между частями)
        if accumulated_text:
            accumulated_text += "\n" + new_text
        else:
            accumulated_text = new_text
        
        # Сохраняем накопленный текст в FSM
        await state.update_data(accumulated_text=accumulated_text)
        
        # Показываем текущую длину
        await message.answer(
            f"✅ Текст добавлен. Текущая длина: {len(accumulated_text)} символов.\n\n"
            f"💡 Отправьте следующую часть или /done для завершения ввода."
        )
    except Exception as e:
        logger.exception("Ошибка при обработке системного промпта: %s", e)
        await message.answer(f"❌ Ошибка при обработке: {e}")


# ----------------------------
# /subscribe и /unsubscribe — админ
# ----------------------------
@user_router.message(F.text.regexp(r"^/subscribe\b"))
async def tg_subscribe(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает команду /subscribe для подписки на webhook от Avito.
    
    Args:
        message: Сообщение с командой
    """
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    # Очищаем состояние, если было активно
    await state.clear()
    
    if not WEBHOOK_URL:
        await message.answer("❗️Не задан PUBLIC_BASE_URL в .env")
        return
    
    creds = _unique_avito_app_creds()
    if not creds:
        ok = subscribe_webhook(WEBHOOK_URL)
        await message.answer("✅ Вебхук зарегистрирован." if ok else "❌ Ошибка регистрации вебхука.")
        return
    ok_count = 0
    for c in creds:
        ok = subscribe_webhook(WEBHOOK_URL, client_id=c["client_id"], client_secret=c["client_secret"])
        ok_count += 1 if ok else 0
    await message.answer(f"🔗 Webhook: успешно {ok_count}/{len(creds)} приложений.")


@user_router.message(F.text.regexp(r"^/unsubscribe\b"))
async def tg_unsubscribe(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает команду /unsubscribe для отписки от webhook от Avito.
    
    Args:
        message: Сообщение с командой
    """
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    # Очищаем состояние, если было активно
    await state.clear()
    
    if not WEBHOOK_URL:
        await message.answer("❗️Не задан PUBLIC_BASE_URL в .env")
        return
    
    creds = _unique_avito_app_creds()
    if not creds:
        ok = unsubscribe_webhook(WEBHOOK_URL)
        await message.answer("✅ Вебхук отключён." if ok else "❌ Ошибка отключения вебхука.")
        return
    ok_count = 0
    for c in creds:
        ok = unsubscribe_webhook(WEBHOOK_URL, client_id=c["client_id"], client_secret=c["client_secret"])
        ok_count += 1 if ok else 0
    await message.answer(f"🔌 Webhook: успешно отключено {ok_count}/{len(creds)} приложений.")


# ----------------------------
# /setmenu — админ
# ----------------------------
# ----------------------------
# /cancel — отмена операции
# ----------------------------
@user_router.message(F.text.regexp(r"^/cancel\b"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает команду /cancel для отмены текущей операции.
    
    Очищает активное состояние FSM.
    
    Args:
        message: Сообщение с командой
        state: FSM контекст для управления состоянием
    """
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("✅ Операция отменена.")
        logger.info("Операция отменена пользователем %d (было состояние: %s)", message.from_user.id, current_state)
    else:
        await message.answer("ℹ️ Нет активной операции для отмены.")


# ----------------------------
# /setmenu — админ
# ----------------------------
async def setup_bot_menu() -> None:
    """
    Устанавливает команды бота в меню Telegram.
    
    Вызывается автоматически при запуске бота и может быть вызвана вручную через /setmenu.
    """
    try:
        # Определяем команды для меню
        commands = [
            BotCommand(command="start", description="Приветствие и описание возможностей"),
        ]
        
        # Команды для администраторов
        admin_commands = [
            BotCommand(command="botstatus", description="Управление ботом (ON/OFF и webhook)"),
            BotCommand(command="accounts", description="Avito аккаунты (режимы/пауза)"),
            BotCommand(command="stats", description="Статистика работы бота"),
            BotCommand(command="agnt_week_overall", description="Анализ истории чатов за неделю"),
            BotCommand(command="faq", description="Управление FAQ"),
            BotCommand(command="staticcontext", description="Управление статическим контекстом"),
            BotCommand(command="dynamiccontext", description="Управление динамическим контекстом"),
            BotCommand(command="systemprompt", description="Управление профилем цифрового ассистента"),
            BotCommand(command="setmenu", description="Обновить меню бота"),
        ]
        
        # Устанавливаем команды для всех пользователей
        await bot.set_my_commands(commands)
        logger.info("Меню бота установлено для всех пользователей")
        
        # Устанавливаем команды для администраторов (scope)
        if ADMINS:
            from aiogram.types import BotCommandScopeChat
            for admin_id in ADMINS:
                try:
                    await bot.set_my_commands(
                        commands + admin_commands,
                        scope=BotCommandScopeChat(chat_id=admin_id)
                    )
                    logger.info("Меню администратора %d установлено", admin_id)
                except Exception as e:
                    logger.warning("Не удалось установить меню для администратора %d: %s", admin_id, e)
        
        logger.info("✅ Меню бота успешно установлено при запуске")
    except Exception as e:
        logger.exception("Ошибка при установке меню бота: %s", e)


@user_router.message(F.text.regexp(r"^/setmenu\b"))
async def cmd_set_menu(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает команду /setmenu для обновления меню бота.
    
    Устанавливает команды бота в меню Telegram.
    
    Args:
        message: Сообщение с командой
        state: FSM контекст для управления состоянием
    """
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    # Очищаем состояние, если было активно
    await state.clear()
    
    try:
        await setup_bot_menu()
        await message.answer("✅ Меню бота успешно обновлено!")
    except Exception as e:
        logger.exception("Ошибка при обновлении меню бота: %s", e)
        await message.answer(f"❌ Ошибка при обновлении меню бота: {e}")


# ----------------------------
# Обработка загрузки файлов с переписками
# ----------------------------
# Обработчик для отмены при ожидании файла знаний
@user_router.message(AdminStates.waiting_for_knowledge_file, F.text.regexp(r"^/cancel\b"))
async def handle_knowledge_file_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет загрузку файла знаний."""
    await state.clear()
    await message.answer("✅ Загрузка отменена.")


# Обработчик для команд при ожидании файла (отменяет операцию)
@user_router.message(AdminStates.waiting_for_knowledge_file, F.text.startswith("/"))
async def handle_knowledge_file_command(message: Message, state: FSMContext) -> None:
    """Отменяет загрузку файла, если отправлена команда."""
    await state.clear()
    logger.info("Команда %s отменила ожидание файла знаний", message.text.split()[0])


@user_router.message(AdminStates.waiting_for_knowledge_file, F.document)
async def handle_knowledge_file(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает загрузку файла с переписками для извлечения знаний.
    
    Поддерживает форматы: JSON, TXT, CSV, HTML
    Автоматически парсит переписки и извлекает знания через LLM.
    
    Args:
        message: Сообщение с документом
        state: FSM контекст для управления состоянием
    """
    if not message.document:
        await message.answer("Пожалуйста, отправьте файл.")
        await state.clear()
        return
    
    document = message.document
    if not document.file_name:
        await message.answer("Файл должен иметь имя.")
        await state.clear()
        return
    
    file_path = os.path.join(DATA_DIR, f"knowledge_upload_{int(time.time())}_{document.file_name}")
    
    try:
        await bot.download(file=document.file_id, destination=file_path)
        logger.info("Админ %d загрузил файл для извлечения знаний: %s", message.from_user.id, file_path)
    except Exception as e:
        logger.exception("Ошибка при загрузке файла: %s", e)
        await message.answer("❌ Ошибка при загрузке файла.")
        await state.clear()
        return
    
    await message.answer("🔄 Парсю файл и извлекаю переписки...")
    
    try:
        from utils.dialogue_parser import parse_dialogues_from_file
        from utils.knowledge_extractor import extract_knowledge_from_dialog
        
        # Парсим файл и извлекаем диалоги
        dialogues = parse_dialogues_from_file(file_path, document.file_name)
        
        if not dialogues:
            await message.answer("❌ Не удалось извлечь переписки из файла. Проверьте формат файла.")
            await state.clear()
            try:
                os.remove(file_path)
            except Exception:
                pass
            return
        
        total_dialogues = len(dialogues)
        await message.answer(
            f"📊 Извлечено диалогов из файла: {total_dialogues}\n"
            f"🤖 Извлекаю знания через LLM... Это может занять время.\n"
            f"💡 Обрабатываю диалоги пакетами для оптимизации..."
        )
        
        # Извлекаем знания из каждого диалога
        all_cards = []
        processed = 0
        errors = 0
        
        # Ограничиваем количество диалогов для обработки (чтобы не перегрузить систему)
        MAX_DIALOGUES_TO_PROCESS = 100
        dialogues_to_process = dialogues[:MAX_DIALOGUES_TO_PROCESS]
        
        if len(dialogues) > MAX_DIALOGUES_TO_PROCESS:
            await message.answer(
                f"⚠️ Файл содержит {len(dialogues)} диалогов. "
                f"Обработаю первые {MAX_DIALOGUES_TO_PROCESS} для оптимизации."
            )
        
        for i, dialogue in enumerate(dialogues_to_process, 1):
            try:
                dialog_id = f"uploaded_file_{int(time.time())}_{i}"
                cards = await extract_knowledge_from_dialog(dialog_id, dialogue)
                if cards:
                    all_cards.extend(cards)
                    processed += 1
                    
                    # Показываем прогресс каждые 10 диалогов
                    if processed % 10 == 0:
                        await message.answer(
                            f"⏳ Обработано диалогов: {processed}/{len(dialogues_to_process)}\n"
                            f"📊 Извлечено карточек: {len(all_cards)}"
                        )
            except Exception as e:
                logger.exception("Ошибка при извлечении знаний из диалога %d: %s", i, e)
                errors += 1
        
        if not all_cards:
            await message.answer("❌ Не удалось извлечь знания из переписок. Попробуйте другой файл.")
            await state.clear()
            try:
                os.remove(file_path)
            except Exception:
                pass
            return
        
        # Сохраняем извлеченные знания
        dialog_id = f"tg_admin_{message.from_user.id}_upload_{int(time.time())}"
        created, updated = upsert_knowledge_cards(all_cards, dialog_id=dialog_id, source="admin_file_upload")
        
        await message.answer(
            f"✅ Обработка завершена!\n\n"
            f"📊 Статистика:\n"
            f"• Обработано диалогов: {processed}/{len(dialogues_to_process)}\n"
            f"• Извлечено карточек: {len(all_cards)}\n"
            f"• Добавлено тем: {created}\n"
            f"• Обновлено тем: {updated}\n"
            f"• Ошибок: {errors}"
        )
        
    except ImportError as e:
        logger.exception("Ошибка импорта модулей: %s", e)
        await message.answer("❌ Ошибка: не удалось загрузить модули парсинга.")
    except Exception as e:
        logger.exception("Ошибка при обработке файла: %s", e)
        await message.answer(f"❌ Ошибка при обработке файла: {e}")
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass
        await state.clear()


# ----------------------------
# Legacy обработчики (для обратной совместимости)
# ----------------------------
@user_router.message(AdminStates.waiting_for_faq_file, F.text.regexp(r"^/cancel\b"))
async def handle_faq_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет загрузку файла знаний (legacy)."""
    await state.clear()
    await message.answer("✅ Загрузка отменена.")


@user_router.message(AdminStates.waiting_for_faq_file, F.text.startswith("/"))
async def handle_faq_command(message: Message, state: FSMContext) -> None:
    """Отменяет загрузку файла, если отправлена команда (legacy)."""
    await state.clear()
    logger.info("Команда %s отменила ожидание FAQ файла", message.text.split()[0])


@user_router.message(AdminStates.waiting_for_faq_file, F.document)
async def handle_faq_file(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает загрузку файла знаний от администратора.
    
    Парсит файл (txt, html, csv) и использует LLM для структурирования
    вопросов и ответов в JSON формат.
    
    Args:
        message: Сообщение с документом
        state: FSM контекст для управления состоянием
    """
    if not message.document:
        await message.answer("Пожалуйста, отправьте файл.")
        await state.clear()
        return
    
    document = message.document
    if not document.file_name:
        await message.answer("Файл должен иметь имя.")
        await state.clear()
        return
    
    file_path = os.path.join(DATA_DIR, f"faq_upload_{int(time.time())}_{document.file_name}")
    
    try:
        await bot.download(file=document.file_id, destination=file_path)
        logger.info("Админ %d загрузил файл FAQ: %s", message.from_user.id, file_path)
    except Exception as e:
        logger.exception("Ошибка при загрузке файла: %s", e)
        await message.answer("Ошибка при загрузке файла.")
        await state.clear()
        return
    
    # Чтение и парсинг файла
    try:
        if document.file_name.endswith(".html"):
            with open(file_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            soup = BeautifulSoup(html_content, "html.parser")
            new_content = soup.get_text(separator="\n").strip()
        else:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                new_content = f.read().strip()
    except Exception as e:
        logger.exception("Ошибка при чтении файла: %s", e)
        await message.answer("Ошибка при чтении файла FAQ.")
        await state.clear()
        # Очищаем временный файл
        try:
            os.remove(file_path)
        except Exception:
            pass
        return

    if not new_content:
        await message.answer("Файл пуст или не удалось извлечь текст.")
        await state.clear()
        return
    
    await message.answer("🤖 Обрабатываю файл и превращаю в знания (knowledge cards)...")

    # 1) Быстрый путь: если файл содержит JSON (cards или Q/A), попробуем разобрать без LLM
    cards: List[Dict[str, Any]] = []
    if document.file_name.endswith(".json"):
        try:
            parsed = json.loads(new_content)
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    # Формат knowledge cards
                    if item.get("topic") and item.get("facts"):
                        topic = str(item.get("topic", "")).strip()
                        facts = item.get("facts")
                        if isinstance(facts, list) and topic and facts:
                            cards.append({"topic": topic, "facts": facts, "tags": item.get("tags") or []})
                    # Формат Q/A
                    elif item.get("question") and item.get("answer"):
                        q = str(item.get("question", "")).strip()
                        a = str(item.get("answer", "")).strip()
                        if q and a:
                            facts = [line.strip("-• \t").strip() for line in a.splitlines() if line.strip()]
                            if not facts:
                                facts = [a]
                            cards.append({"topic": q, "facts": facts, "tags": []})
        except Exception:
            cards = []

    # 2) Если не получилось — используем LLM и превращаем произвольный текст в cards
    if not cards:
        cards = await _knowledge_cards_from_text_via_llm(new_content)

    if not cards:
        await message.answer("❌ Не удалось выделить знания из файла. Попробуйте дать более явный текст/правила/прайс.")
        await state.clear()
        try:
            os.remove(file_path)
        except Exception:
            pass
        return

    try:
        dialog_id = f"tg_admin_{message.from_user.id}"
        created, updated = upsert_knowledge_cards(cards, dialog_id=dialog_id, source="admin_upload")
        await message.answer(f"✅ Готово. Добавлено тем: {created}, обновлено: {updated}.")
    except Exception as e:
        logger.exception("Ошибка при сохранении knowledge cards: %s", e)
        await message.answer(f"❌ Ошибка при сохранении базы знаний: {e}")
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass
        await state.clear()


# Функции _load_faq_safe, _save_faq_safe, _validate_faq_entry, _add_faq_entry_safe,
# _add_faq_entries_batch, _parse_faq_text удалены - теперь используются из utils.faq_utils


# Обработчик для добавления знаний текстом (legacy, для обратной совместимости)
@user_router.message(AdminStates.waiting_for_faq_text)
async def handle_faq_text(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает добавление знаний текстом (legacy обработчик).
    
    Использует новую систему извлечения знаний из текста.
    
    Args:
        message: Сообщение с текстом
        state: FSM контекст для управления состоянием
    """
    # Проверяем, не является ли сообщение командой
    if message.text and message.text.startswith("/"):
        await state.clear()
        logger.info("Команда %s отменила ожидание текста знаний", message.text.split()[0])
        return
    
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение или /cancel для отмены.")
        return
    
    try:
        raw = (message.text or "").strip()
        if not raw:
            await message.answer("❌ Текст не может быть пустым. Отправьте текст или /cancel для отмены.")
            return

        await message.answer("🤖 Превращаю текст в знания (knowledge cards)...")

        # Используем новую систему извлечения знаний
        try:
            from utils.knowledge_cards import add_knowledge_from_text
            
            count, topics = add_knowledge_from_text(
                raw,
                source="admin_manual",
                dialog_id=f"tg_admin_{message.from_user.id}"
            )
            
            if count > 0:
                topics_str = "\n".join([f"• {t}" for t in topics[:10]])
                if len(topics) > 10:
                    topics_str += f"\n... и еще {len(topics) - 10}"
                await message.answer(
                    f"✅ Извлечено знаний: <b>{count}</b> карточек\n\n"
                    f"📋 Темы:\n{topics_str}",
                    parse_mode="HTML"
                )
            else:
                # Fallback на LLM если простой парсинг не сработал
                cards = await _knowledge_cards_from_text_via_llm(raw)
                if not cards:
                    await message.answer("ℹ️ Не удалось обработать текст. Попробуйте ещё раз.")
                    return
                
                dialog_id = f"tg_admin_{message.from_user.id}"
                created, updated = upsert_knowledge_cards(cards, dialog_id=dialog_id, source="admin_text")
                await message.answer(f"✅ Готово. Добавлено тем: {created}, обновлено: {updated}.")
        except ImportError:
            # Fallback на старый метод
            cards = await _knowledge_cards_from_text_via_llm(raw)
            if not cards:
                await message.answer("ℹ️ Не удалось обработать текст. Попробуйте ещё раз.")
                return
            
            dialog_id = f"tg_admin_{message.from_user.id}"
            created, updated = upsert_knowledge_cards(cards, dialog_id=dialog_id, source="admin_text")
            await message.answer(f"✅ Готово. Добавлено тем: {created}, обновлено: {updated}.")
            
    except Exception as e:
        logger.exception("Ошибка при добавлении знаний текстом: %s", e)
        await message.answer(f"❌ Ошибка при добавлении: {e}")
    finally:
        await state.clear()


# ----------------------------
# Обработка документов (автоматическое предложение добавить в базу знаний)
# ----------------------------
@user_router.message(F.document)
async def handle_document(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает отправку документов.
    
    Если отправитель - админ, предлагает добавить файл в базу знаний.
    Если пользователь уже в состоянии ожидания файла - пропускает обработку (сработает другой обработчик).
    """
    # Проверяем, не находимся ли мы уже в состоянии ожидания файла
    current_state = await state.get_state()
    if current_state in [
        AdminStates.waiting_for_knowledge_file,
        AdminStates.waiting_for_faq_file
    ]:
        # Пропускаем - сработает специализированный обработчик
        return
    
    # Проверяем, является ли пользователь админом
    if not _check_admin(message.from_user.id):
        # Для обычных пользователей просто игнорируем документы
        return
    
    if not message.document or not message.document.file_name:
        return
    
    document = message.document
    file_name = document.file_name
    
    # Проверяем, поддерживается ли формат файла
    supported_extensions = [".json", ".txt", ".csv", ".html", ".md"]
    if not any(file_name.lower().endswith(ext) for ext in supported_extensions):
        await message.answer(
            f"📎 Получен файл: <b>{file_name}</b>\n\n"
            f"⚠️ Поддерживаются только файлы: JSON, TXT, CSV, HTML, MD\n"
            f"💡 Для добавления в базу знаний используйте команду /knowledge",
            parse_mode="HTML"
        )
        return
    
    # Предлагаем добавить в базу знаний
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, добавить в базу знаний", callback_data="kb_confirm_upload"),
            InlineKeyboardButton(text="❌ Нет", callback_data="kb_cancel_upload")
        ]
    ])
    
    await message.answer(
        f"📎 Получен файл: <b>{file_name}</b>\n\n"
        f"💡 Хотите добавить этот файл в базу знаний?\n\n"
        f"📌 Бот автоматически извлечет знания из переписок в файле.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    
    # Сохраняем информацию о файле в состоянии
    await state.update_data(
        pending_file_id=document.file_id,
        pending_file_name=file_name
    )


@user_router.callback_query(F.data == "kb_confirm_upload")
async def callback_kb_confirm_upload(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик подтверждения загрузки файла в базу знаний."""
    await callback.answer()
    
    if not _check_admin(callback.from_user.id):
        await callback.message.answer("⛔️ Недостаточно прав.")
        return
    
    data = await state.get_data()
    file_id = data.get("pending_file_id")
    file_name = data.get("pending_file_name")
    
    if not file_id or not file_name:
        await callback.message.answer("❌ Информация о файле не найдена. Попробуйте отправить файл снова.")
        await state.clear()
        return
    
    # Скачиваем файл
    file_path = os.path.join(DATA_DIR, f"knowledge_upload_{int(time.time())}_{file_name}")
    
    try:
        await bot.download(file=file_id, destination=file_path)
        logger.info("Админ %d подтвердил загрузку файла для извлечения знаний: %s", callback.from_user.id, file_path)
    except Exception as e:
        logger.exception("Ошибка при загрузке файла: %s", e)
        await callback.message.answer("❌ Ошибка при загрузке файла.")
        await state.clear()
        return
    
    await callback.message.edit_text("🔄 Парсю файл и извлекаю переписки...")
    
    try:
        from utils.dialogue_parser import parse_dialogues_from_file
        from utils.knowledge_extractor import extract_knowledge_from_dialog
        
        # Парсим файл и извлекаем диалоги
        dialogues = parse_dialogues_from_file(file_path, file_name)
        
        if not dialogues:
            await callback.message.edit_text("❌ Не удалось извлечь переписки из файла. Проверьте формат файла.")
            await state.clear()
            try:
                os.remove(file_path)
            except Exception:
                pass
            return
        
        total_dialogues = len(dialogues)
        await callback.message.edit_text(
            f"📊 Извлечено диалогов из файла: {total_dialogues}\n"
            f"🤖 Извлекаю знания через LLM... Это может занять время.\n"
            f"💡 Обрабатываю диалоги пакетами для оптимизации..."
        )
        
        # Извлекаем знания из каждого диалога
        all_cards = []
        processed = 0
        errors = 0
        
        # Ограничиваем количество диалогов для обработки
        MAX_DIALOGUES_TO_PROCESS = 100
        dialogues_to_process = dialogues[:MAX_DIALOGUES_TO_PROCESS]
        
        if len(dialogues) > MAX_DIALOGUES_TO_PROCESS:
            await callback.message.answer(
                f"⚠️ Файл содержит {len(dialogues)} диалогов. "
                f"Обработаю первые {MAX_DIALOGUES_TO_PROCESS} для оптимизации."
            )
        
        for i, dialogue in enumerate(dialogues_to_process, 1):
            try:
                dialog_id = f"uploaded_file_{int(time.time())}_{i}"
                cards = await extract_knowledge_from_dialog(dialog_id, dialogue)
                if cards:
                    all_cards.extend(cards)
                    processed += 1
                    
                    # Показываем прогресс каждые 10 диалогов
                    if processed % 10 == 0:
                        await callback.message.answer(
                            f"⏳ Обработано диалогов: {processed}/{len(dialogues_to_process)}\n"
                            f"📊 Извлечено карточек: {len(all_cards)}"
                        )
            except Exception as e:
                logger.exception("Ошибка при извлечении знаний из диалога %d: %s", i, e)
                errors += 1
        
        if not all_cards:
            await callback.message.edit_text("❌ Не удалось извлечь знания из переписок. Попробуйте другой файл.")
            await state.clear()
            try:
                os.remove(file_path)
            except Exception:
                pass
            return
        
        # Сохраняем извлеченные знания
        dialog_id = f"tg_admin_{callback.from_user.id}_upload_{int(time.time())}"
        created, updated = upsert_knowledge_cards(all_cards, dialog_id=dialog_id, source="admin_file_upload")
        
        await callback.message.edit_text(
            f"✅ Обработка завершена!\n\n"
            f"📊 Статистика:\n"
            f"• Обработано диалогов: {processed}/{len(dialogues_to_process)}\n"
            f"• Извлечено карточек: {len(all_cards)}\n"
            f"• Добавлено тем: {created}\n"
            f"• Обновлено тем: {updated}\n"
            f"• Ошибок: {errors}"
        )
        
    except ImportError as e:
        logger.exception("Ошибка импорта модулей: %s", e)
        await callback.message.edit_text("❌ Ошибка: не удалось загрузить модули парсинга.")
    except Exception as e:
        logger.exception("Ошибка при обработке файла: %s", e)
        await callback.message.edit_text(f"❌ Ошибка при обработке файла: {e}")
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass
        await state.clear()


@user_router.callback_query(F.data == "kb_cancel_upload")
async def callback_kb_cancel_upload(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик отмены загрузки файла."""
    await callback.answer("Отменено")
    await callback.message.edit_text("❌ Загрузка файла отменена.")
    await state.clear()


# ----------------------------
# Ответ пользователю в ТГ (через единый responder)
# ----------------------------
@user_router.message(F.text)
async def handle_user_message(message: Message) -> None:
    """
    Обрабатывает обычные текстовые сообщения от пользователей.
    
    Генерирует ответ через LLM на основе базы знаний, истории диалога и статического контекста.
    
    Args:
        message: Текстовое сообщение от пользователя
    """
    if not message.text:
        return
    
    logger.info(
        "handle_user_message called: user_id=%d, text_length=%d",
        message.from_user.id, len(message.text)
    )
    
    raw_text = message.text.strip()
    
    # Игнорируем системные сообщения
    if any(raw_text.startswith(prefix) for prefix in SYSTEM_MESSAGE_PREFIXES):
        logger.debug("Skipping system message")
        return

    # Извлекаем dialog_id (может быть в специальном формате)
    m = DIALOG_ID_PATTERN.search(raw_text)
    dialog_id = m.group(1) if m else f"tg_{message.from_user.id}"

    # Очищаем хвост id из текста
    clean_text = DIALOG_ID_CLEANUP_PATTERN.sub("", raw_text).strip()

    # Извлекаем имя и вопрос (если есть формат "Имя: вопрос")
    name_m = NAME_PATTERN.search(clean_text)
    if name_m:
        user_name = name_m.group(1)
        user_question = name_m.group(2).strip()
    else:
        user_name = None
        user_question = clean_text

    # Генерируем ответ через единый модуль
    logger.info(
        "Calling generate_reply for dialog_id=%s, user_question_length=%d",
        dialog_id, len(user_question)
    )
    
    try:
        answer, _meta = await generate_reply(
            dialog_id=dialog_id,
            incoming_text=user_question,
            user_name=user_name,
        )
        
        logger.info(
            "generate_reply returned for dialog_id=%s: answer=%s, meta=%s",
            dialog_id,
            "None" if answer is None else f"length={len(answer)}",
            _meta
        )
    except Exception as e:
        logger.exception("Ошибка при генерации ответа: %s", e)
        answer = None
    
    # Если произошла ошибка при генерации ответа - не отправляем ничего клиенту
    if answer is None:
        logger.warning("Failed to generate reply for dialog_id=%s - not sending message to client", dialog_id)
        await message.answer("Извините, произошла техническая ошибка. Попробуйте позже или обратитесь к менеджеру.")
        return
    
    logger.info("Sending answer to Telegram user for dialog_id=%s, answer_length=%d", dialog_id, len(answer))
    
    try:
        await message.reply(answer)
        logger.info("✅ Ответ успешно отправлен в Telegram для dialog_id=%s", dialog_id)
        
        # Сохраняем ответ в историю ТОЛЬКО после успешной отправки
        try:
            usage = _meta.get("usage") if _meta and "usage" in _meta else None
            save_assistant_message(dialog_id, answer, usage)
            logger.info("Saved chat history for dialog_id=%s (after successful send)", dialog_id)
        except Exception as e:
            logger.warning("Failed to save chat history after sending to Telegram: %s", e)
    except Exception as e:
        logger.exception("Ошибка при отправке ответа в Telegram: %s", e)
        await message.answer("Извините, произошла ошибка при отправке ответа.")
