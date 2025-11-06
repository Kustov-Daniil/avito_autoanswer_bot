"""
Модуль обработчиков команд и сообщений для Telegram бота.

Содержит обработчики для:
- Команд пользователей (/start)
- Административных команд (/uploadfaq, /staticcontext, /subscribe, /unsubscribe)
- Обычных сообщений пользователей (генерация ответов через LLM)
- Обратной связи (👍 👎)
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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import AsyncOpenAI
from bs4 import BeautifulSoup

from create_bot import bot
from config import (
    FAQ_PATH, STATIC_CONTEXT_PATH, DYNAMIC_CONTEXT_PATH, SYSTEM_PROMPT_PATH, CHAT_HISTORY_PATH,
    LLM_MODEL, TEMPERATURE, OPENAI_API_KEY, ADMINS, WEBHOOK_URL, DATA_DIR, SIGNAL_PHRASES,
    MANAGER_COST_PER_HOUR, USD_RATE
)
from avito_sessions import set_bot_enabled, is_bot_enabled, get_llm_model, set_llm_model
from responder import generate_reply
from avito_api import subscribe_webhook, unsubscribe_webhook

# Константы
MAX_FAQ_CHUNK_SIZE: int = 6000
SYSTEM_MESSAGE_PREFIXES: List[str] = ["Системное:", "Сообщение отправлено"]
DIALOG_ID_PATTERN: re.Pattern = re.compile(r";([0-9]+:m:[^:]+):")
DIALOG_ID_CLEANUP_PATTERN: re.Pattern = re.compile(r"[a-z0-9]+;[0-9]+:m:[^:]+:[0-9]+$")
NAME_PATTERN: re.Pattern = re.compile(r"^([\wА-Яа-яёЁ]+):\s*(.+)")
SUBSCRIBE_PATTERN: re.Pattern = re.compile(r"^/subscribe\b")
UNSUBSCRIBE_PATTERN: re.Pattern = re.compile(r"^/unsubscribe\b")

# Инициализация
user_router = Router()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

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

# Временное хранилище для рейтинга (в production можно использовать Redis/БД)
TEMP_QA: Dict[str, Dict[str, str]] = {}


class AdminStates(StatesGroup):
    """Состояния FSM для административных команд."""
    waiting_for_faq_file = State()
    waiting_for_faq_text = State()
    waiting_for_static_context = State()
    waiting_for_dynamic_context = State()
    waiting_for_system_prompt = State()


def _check_admin(user_id: int) -> bool:
    """
    Проверяет, является ли пользователь администратором.
    
    Args:
        user_id: ID пользователя в Telegram
        
    Returns:
        True если пользователь администратор, False иначе
    """
    return user_id in ADMINS if ADMINS else False


def _calculate_token_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """
    Рассчитывает стоимость использования токенов для указанной модели.
    
    Args:
        model: Название модели (например, "gpt-4o", "gpt-5", "gpt-5-mini")
        prompt_tokens: Количество токенов в промпте
        completion_tokens: Количество токенов в ответе
        
    Returns:
        Стоимость в долларах США
    """
    # Цены на модели OpenAI (на 1M токенов)
    # Источник: https://openai.com/api/pricing/ (примерные цены, нужно обновлять)
    pricing = {
        "gpt-4o": {
            "input": 2.50,   # $2.50 за 1M input tokens
            "output": 10.00  # $10.00 за 1M output tokens
        },
        "gpt-5": {
            "input": 2.50,   # Предположительно похожие цены
            "output": 10.00
        },
        "gpt-5-mini": {
            "input": 0.15,   # $0.15 за 1M input tokens (дешевле)
            "output": 0.60   # $0.60 за 1M output tokens
        }
    }
    
    # Получаем цены для модели или используем значения по умолчанию
    model_pricing = pricing.get(model, pricing["gpt-4o"])
    
    # Рассчитываем стоимость
    input_cost = (prompt_tokens / 1_000_000) * model_pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * model_pricing["output"]
    
    return input_cost + output_cost


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
        - faq_total: общее количество вопросов в FAQ
        - faq_admin: количество вопросов, добавленных админом
        - faq_manager: количество вопросов, добавленных менеджером
        - faq_manager_like: количество вопросов, лайкнутых менеджером
    """
    try:
        from responder import _load_json
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
    except Exception as e:
        logger.exception("Ошибка при загрузке chat_history для статистики: %s", e)
        chat_history = {}
    
    # Загружаем FAQ для статистики (используем безопасную загрузку)
    try:
        faq_data, _ = _load_faq_safe()
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
                        cost_usd = _calculate_token_cost(model, prompt_tokens, completion_tokens)
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
    
    # Получаем текущую модель LLM
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    
    # Создаем inline кнопки в зависимости от текущего статуса
    buttons = []
    
    # Кнопка включения/выключения бота
    if current_status:
        buttons.append([InlineKeyboardButton(text="🔴 Выключить бота", callback_data="bot_off")])
    else:
        buttons.append([InlineKeyboardButton(text="🟢 Включить бота", callback_data="bot_on")])
    
    # Кнопки для выбора модели LLM
    buttons.append([InlineKeyboardButton(text="🤖 Выбрать модель LLM", callback_data="llm_model_menu")])
    
    # Кнопки для webhook
    buttons.append([
        InlineKeyboardButton(text="🔗 Подключить webhook", callback_data="webhook_subscribe"),
        InlineKeyboardButton(text="🔌 Отключить webhook", callback_data="webhook_unsubscribe"),
    ])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(
        f"🤖 Управление ботом\n\n"
        f"📊 Текущий статус бота: {status_text}\n"
        f"🤖 Текущая модель LLM: {current_model_name}\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )


@user_router.callback_query(F.data == "bot_on")
async def callback_bot_on(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Включить бота'."""
    await callback.answer()
    set_bot_enabled(True)
    
    # Обновляем меню с новым статусом
    status_text = "🟢 ВКЛЮЧЕН"
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    
    buttons = []
    buttons.append([InlineKeyboardButton(text="🔴 Выключить бота", callback_data="bot_off")])
    buttons.append([InlineKeyboardButton(text="🤖 Выбрать модель LLM", callback_data="llm_model_menu")])
    buttons.append([
        InlineKeyboardButton(text="🔗 Подключить webhook", callback_data="webhook_subscribe"),
        InlineKeyboardButton(text="🔌 Отключить webhook", callback_data="webhook_unsubscribe"),
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(
        f"🤖 Управление ботом\n\n"
        f"📊 Текущий статус бота: {status_text}\n"
        f"🤖 Текущая модель LLM: {current_model_name}\n\n"
        "✅ Бот включен. Теперь он будет отвечать на сообщения из Avito.\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )


@user_router.callback_query(F.data == "bot_off")
async def callback_bot_off(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Выключить бота'."""
    await callback.answer()
    set_bot_enabled(False)
    
    # Обновляем меню с новым статусом
    status_text = "🔴 ВЫКЛЮЧЕН"
    current_model = get_llm_model("gpt-4o")
    model_display_names = {
        "gpt-5": "Chat GPT 5",
        "gpt-5-mini": "Chat GPT 5 mini",
        "gpt-4o": "Chat GPT 4o"
    }
    current_model_name = model_display_names.get(current_model, current_model)
    
    buttons = []
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
        f"🤖 Текущая модель LLM: {current_model_name}\n\n"
        "⛔️ Бот выключен. Он не будет отвечать на сообщения из Avito.\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )


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
    
    ok = subscribe_webhook(WEBHOOK_URL)
    if ok:
        await callback.message.answer("✅ Вебхук зарегистрирован.")
    else:
        await callback.message.answer("❌ Ошибка регистрации вебхука.")


@user_router.callback_query(F.data == "webhook_unsubscribe")
async def callback_webhook_unsubscribe(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Отключить webhook'."""
    await callback.answer()
    
    if not WEBHOOK_URL:
        await callback.message.answer("❗️ Не задан PUBLIC_BASE_URL в .env")
        return
    
    ok = unsubscribe_webhook(WEBHOOK_URL)
    if ok:
        await callback.message.answer("✅ Вебхук отключён.")
    else:
        await callback.message.answer("❌ Ошибка отключения вебхука.")


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
        f"🤖 Текущая модель LLM: {current_model_name}\n\n"
        "Выберите действие:",
        reply_markup=keyboard
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
        stats = _calculate_stats()
        
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
# /faq — управление FAQ
# ----------------------------
@user_router.message(F.text.regexp(r"^/faq\b"))
async def cmd_faq_menu(message: Message, state: FSMContext) -> None:
    """
    Показывает меню управления FAQ.
    
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
        [
            InlineKeyboardButton(text="📥 Добавить текстом", callback_data="faq_add_text"),
            InlineKeyboardButton(text="📎 Загрузить файлом", callback_data="faq_upload_file"),
        ],
        [
            InlineKeyboardButton(text="📤 Скачать FAQ", callback_data="faq_download"),
            InlineKeyboardButton(text="👁️ Просмотреть", callback_data="faq_view"),
        ],
    ])
    
    await message.answer(
        "📚 Управление FAQ\n\n"
        "📌 <b>FAQ</b> — база правильных ответов на вопросы, которые уже были отвечены "
        "или заранее подготовлены, чтобы бот ориентировался на них при ответе.\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )


@user_router.callback_query(F.data == "faq_add_text")
async def callback_faq_add_text(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Добавить текстом'."""
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "📝 Отправьте текст для добавления в FAQ (нарастающим итогом).\n\n"
        "📌 <b>FAQ</b> — база правильных ответов на вопросы, которые уже были отвечены "
        "или заранее подготовлены, чтобы бот ориентировался на них при ответе.\n\n"
        "Поддерживаемые форматы:\n"
        "• <b>Структурированный:</b> Q: вопрос\nA: ответ\n\n"
        "• <b>JSON:</b> [{\"question\": \"...\", \"answer\": \"...\"}]\n\n"
        "• <b>Свободный формат:</b> просто текст - бот сам создаст структуру с помощью LLM\n\n"
        "💡 Для отмены отправьте /cancel"
    )
    await state.set_state(AdminStates.waiting_for_faq_text)


@user_router.callback_query(F.data == "faq_upload_file")
async def callback_faq_upload_file(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Загрузить файлом'."""
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "📎 Отправьте файл FAQ (txt, html или csv) для полной перезаписи.\n\n"
        "💡 Для отмены отправьте /cancel"
    )
    await state.set_state(AdminStates.waiting_for_faq_file)


@user_router.callback_query(F.data == "faq_download")
async def callback_faq_download(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Скачать FAQ'."""
    await callback.answer()
    try:
        if os.path.exists(FAQ_PATH):
            # Используем FSInputFile для отправки файла
            document = FSInputFile(FAQ_PATH, filename="faq.json")
            await callback.message.answer_document(
                document=document,
                caption="📥 Файл FAQ"
            )
        else:
            await callback.message.answer("❌ Файл FAQ не найден.")
    except Exception as e:
        logger.exception("Ошибка при скачивании FAQ: %s", e)
        await callback.message.answer(f"❌ Ошибка при скачивании FAQ: {e}")


@user_router.callback_query(F.data == "faq_view")
async def callback_faq_view(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Просмотреть FAQ'."""
    await callback.answer()
    try:
        if os.path.exists(FAQ_PATH):
            with open(FAQ_PATH, "r", encoding="utf-8") as f:
                faq_data = json.load(f)
            if faq_data:
                preview = f"📚 FAQ содержит {len(faq_data)} вопросов/ответов:\n\n"
                for i, item in enumerate(faq_data[:5], 1):
                    q = item.get("question", "")[:50]
                    a = item.get("answer", "")[:50]
                    preview += f"{i}. Q: {q}...\n   A: {a}...\n\n"
                if len(faq_data) > 5:
                    preview += f"... и еще {len(faq_data) - 5} вопросов"
                await callback.message.answer(preview)
            else:
                await callback.message.answer("📚 FAQ пуст.")
        else:
            await callback.message.answer("❌ Файл FAQ не найден.")
    except Exception as e:
        logger.exception("Ошибка при просмотре FAQ: %s", e)
        await callback.message.answer(f"❌ Ошибка при просмотре FAQ: {e}")


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
    
    ok = subscribe_webhook(WEBHOOK_URL)
    await message.answer("✅ Вебхук зарегистрирован." if ok else "❌ Ошибка регистрации вебхука.")


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
    
    ok = unsubscribe_webhook(WEBHOOK_URL)
    await message.answer("✅ Вебхук отключён." if ok else "❌ Ошибка отключения вебхука.")


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
        # Определяем команды для меню
        commands = [
            BotCommand(command="start", description="Приветствие и описание возможностей"),
        ]
        
        # Команды для администраторов
        admin_commands = [
            BotCommand(command="botstatus", description="Управление ботом (ON/OFF и webhook)"),
            BotCommand(command="stats", description="Статистика работы бота"),
            BotCommand(command="faq", description="Управление FAQ"),
            BotCommand(command="staticcontext", description="Управление статическим контекстом"),
            BotCommand(command="dynamiccontext", description="Управление динамическим контекстом"),
            BotCommand(command="systemprompt", description="Управление профилем цифрового ассистента"),
            BotCommand(command="setmenu", description="Обновить меню бота"),
        ]
        
        # Устанавливаем команды для всех пользователей
        await bot.set_my_commands(commands)
        logger.info("Меню бота обновлено пользователем %d", message.from_user.id)
        
        # Устанавливаем команды для администраторов (scope)
        if ADMINS:
            from aiogram.types import BotCommandScopeChat
            for admin_id in ADMINS:
                try:
                    await bot.set_my_commands(
                        commands + admin_commands,
                        scope=BotCommandScopeChat(chat_id=admin_id)
                    )
                    logger.info("Меню администратора %d обновлено", admin_id)
                except Exception as e:
                    logger.warning("Не удалось установить меню для администратора %d: %s", admin_id, e)
        
        await message.answer("✅ Меню бота успешно обновлено!")
    except Exception as e:
        logger.exception("Ошибка при обновлении меню бота: %s", e)
        await message.answer(f"❌ Ошибка при обновлении меню бота: {e}")


# ----------------------------
# Обработка FAQ файла
# ----------------------------
# Обработчик для отмены при ожидании FAQ файла
@user_router.message(AdminStates.waiting_for_faq_file, F.text.regexp(r"^/cancel\b"))
async def handle_faq_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет загрузку FAQ файла."""
    await state.clear()
    await message.answer("✅ Загрузка FAQ отменена.")


# Обработчик для команд при ожидании FAQ файла (отменяет операцию)
@user_router.message(AdminStates.waiting_for_faq_file, F.text.startswith("/"))
async def handle_faq_command(message: Message, state: FSMContext) -> None:
    """Отменяет загрузку FAQ, если отправлена команда."""
    await state.clear()
    logger.info("Команда %s отменила ожидание FAQ файла", message.text.split()[0])


@user_router.message(AdminStates.waiting_for_faq_file, F.document)
async def handle_faq_file(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает загрузку FAQ файла от администратора.
    
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
    
    # Пробуем парсить как JSON файл
    new_faq = []
    try:
        # Если файл JSON, парсим напрямую
        if document.file_name.endswith(".json"):
            parsed = json.loads(new_content)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "question" in item and "answer" in item:
                        new_faq.append({
                            "question": str(item["question"]).strip(),
                            "answer": str(item["answer"]).strip(),
                            "source": "admin"  # Добавлено админом через загрузку файла
                        })
        else:
            # Для других форматов парсим текст
            parsed_faq = _parse_faq_text(new_content)
            # Добавляем source для всех записей
            new_faq = [
                {**item, "source": "admin"}  # Добавлено админом через загрузку файла
                for item in parsed_faq
            ]
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Не удалось распарсить файл как JSON, пробуем как текст: %s", e)
        parsed_faq = _parse_faq_text(new_content)
        # Добавляем source для всех записей
        new_faq = [
            {**item, "source": "admin"}  # Добавлено админом через загрузку файла
            for item in parsed_faq
        ]
    
    # Если не удалось распарсить напрямую, используем LLM для структурирования
    if not new_faq:
        logger.info("Не удалось распарсить FAQ из файла напрямую, используем LLM для структурирования")
        await message.answer("🤖 Обрабатываю файл с помощью LLM для создания структуры FAQ...")
        
        try:
            # Разделяем контент на части для обработки LLM (если файл большой)
            chunks = [
                new_content[i:i + MAX_FAQ_CHUNK_SIZE]
                for i in range(0, len(new_content), MAX_FAQ_CHUNK_SIZE)
            ]
            logger.info("Файл разделен на %d частей для обработки LLM", len(chunks))
            
            all_new_faq = []
            
            # Обрабатываем каждую часть через LLM
            for idx, chunk in enumerate(chunks, start=1):
                prompt = f"""
Ты — эксперт по международным визам. 
Вот часть текста для добавления в FAQ (часть {idx} из {len(chunks)}):

{chunk}

Задача: структурировать вопросы и ответы в JSON массив вида:
[
  {{"question": "...", "answer": "..."}}
]

Извлеки все вопросы и ответы из текста. Если в тексте нет явных вопросов, создай их на основе содержания.
Не дублируй одинаковые вопросы. 
Отвечай только JSON — без текста, без комментариев.
"""
                try:
                    use_temperature = LLM_MODEL not in ["gpt-5-mini", "gpt-5"]
                    
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
                    llm_response = response.choices[0].message.content.strip()
                    
                    # Извлекаем JSON из ответа
                    match = re.search(r"\[.*\]", llm_response, re.DOTALL)
                    if match:
                        chunk_faq = json.loads(match.group(0))
                        chunk_faq = [
                            {
                                "question": str(i.get("question", "")).strip(),
                                "answer": str(i.get("answer", "")).strip(),
                                "source": "admin"  # Добавлено админом через загрузку файла
                            }
                            for i in chunk_faq
                            if isinstance(i, dict) and i.get("question") and i.get("answer")
                        ]
                        all_new_faq.extend(chunk_faq)
                        logger.info("Обработана часть %d/%d, получено %d записей.", idx, len(chunks), len(chunk_faq))
                    else:
                        logger.warning("Часть %d не вернула корректный JSON", idx)
                except Exception as e:
                    logger.exception("Ошибка LLM при обработке части %d: %s", idx, e)
                    continue

            new_faq = all_new_faq
            
            if not new_faq:
                await message.answer(
                    "❌ Не удалось создать структуру FAQ из файла.\n\n"
                    "Файл должен содержать:\n"
                    "• JSON: [{\"question\": \"...\", \"answer\": \"...\"}]\n"
                    "• Или текст в формате Q: ... A: ...\n"
                    "• Или свободный текст (будет обработан LLM)"
                )
                await state.clear()
                try:
                    os.remove(file_path)
                except Exception:
                    pass
                return
        except Exception as e:
            logger.exception("Ошибка при обработке файла через LLM: %s", e)
            await message.answer(
                f"❌ Ошибка при обработке файла через LLM: {e}\n\n"
                "Попробуйте использовать структурированный формат:\n"
                "• JSON: [{\"question\": \"...\", \"answer\": \"...\"}]\n"
                "• Или текст в формате Q: ... A: ..."
            )
            await state.clear()
            try:
                os.remove(file_path)
            except Exception:
                pass
            return
    
    # При загрузке файлом полностью перезаписываем FAQ
    try:
        os.makedirs(os.path.dirname(FAQ_PATH), exist_ok=True)
        with open(FAQ_PATH, "w", encoding="utf-8") as f:
            json.dump(new_faq, f, ensure_ascii=False, indent=2)
        
        logger.info("✅ FAQ полностью перезаписан из файла пользователем %d, добавлено %d записей", 
                   message.from_user.id, len(new_faq))
        await message.answer(f"✅ FAQ полностью перезаписан из файла. Добавлено {len(new_faq)} записей.")
    except Exception as e:
        logger.exception("Ошибка при сохранении FAQ: %s", e)
        await message.answer("Ошибка при сохранении FAQ.")
    finally:
        # Очищаем временный файл
        try:
            os.remove(file_path)
        except Exception:
            pass
    await state.clear()


def _load_faq_safe() -> tuple[List[Dict[str, Any]], int]:
    """
    Безопасно загружает FAQ с защитой от потери данных.
    
    Returns:
        Кортеж (список FAQ, количество записей)
    """
    from responder import _load_json
    backup_path = f"{FAQ_PATH}.backup"
    original_faq_count = 0
    current_faq = None
    
    try:
        with open(FAQ_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                logger.warning("FAQ файл пуст, пробуем восстановить из резервной копии")
                raise ValueError("FAQ файл пуст")
            current_faq = json.loads(content)
        # Проверяем, что это список
        if not isinstance(current_faq, list):
            logger.error("FAQ файл не содержит список, пробуем восстановить из резервной копии")
            raise ValueError("FAQ файл не содержит список")
        else:
            original_faq_count = len(current_faq)
            logger.debug("Загружен FAQ: %d записей", original_faq_count)
    except FileNotFoundError:
        logger.warning("FAQ файл не найден, пробуем восстановить из резервной копии")
        current_faq = None
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Ошибка при загрузке FAQ файла: %s, пробуем восстановить из резервной копии", e)
        # Пробуем исправить JSON (убрать лишние запятые в конце)
        try:
            with open(FAQ_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            # Убираем лишние запятые перед закрывающими скобками
            content = re.sub(r',\s*\]', ']', content)
            content = re.sub(r',\s*\}', '}', content)
            current_faq = json.loads(content)
            if isinstance(current_faq, list) and len(current_faq) > 0:
                original_faq_count = len(current_faq)
                logger.info("✅ FAQ исправлен автоматически: %d записей", original_faq_count)
                # Сохраняем исправленный FAQ
                with open(FAQ_PATH, "w", encoding="utf-8") as f:
                    json.dump(current_faq, f, ensure_ascii=False, indent=2)
            else:
                current_faq = None
        except Exception as fix_e:
            logger.warning("Не удалось автоматически исправить FAQ: %s", fix_e)
            current_faq = None
    except Exception as e:
        logger.exception("Неожиданная ошибка при загрузке FAQ: %s", e)
        current_faq = None
    
    # Если не удалось загрузить FAQ, пробуем восстановить из резервной копии
    if current_faq is None:
        if os.path.exists(backup_path):
            try:
                logger.info("Восстанавливаю FAQ из резервной копии: %s", backup_path)
                with open(backup_path, "r", encoding="utf-8") as f:
                    current_faq = json.load(f)
                if isinstance(current_faq, list):
                    original_faq_count = len(current_faq)
                    logger.info("✅ FAQ восстановлен из резервной копии: %d записей", original_faq_count)
                    # Восстанавливаем основной файл из резервной копии
                    shutil.copy2(backup_path, FAQ_PATH)
                else:
                    logger.error("Резервная копия FAQ не содержит список")
                    current_faq = []
            except Exception as restore_e:
                logger.exception("Не удалось восстановить FAQ из резервной копии: %s", restore_e)
                current_faq = []
        else:
            logger.warning("Резервная копия FAQ не найдена, создаем новый FAQ")
            current_faq = []
    
    if not isinstance(current_faq, list):
        logger.warning("FAQ данные не являются списком после всех попыток загрузки, возвращаем пустой список")
        current_faq = []
        original_faq_count = 0
    
    logger.debug("_load_faq_safe возвращает: %d записей (original_count: %d)", len(current_faq), original_faq_count)
    return current_faq, original_faq_count


def _save_faq_safe(faq_data: List[Dict[str, Any]], original_count: int) -> bool:
    """
    Безопасно сохраняет FAQ с проверкой целостности.
    
    Args:
        faq_data: Список FAQ записей для сохранения
        original_count: Оригинальное количество записей (для проверки)
        
    Returns:
        True если сохранение успешно, False иначе
    """
    backup_path = f"{FAQ_PATH}.backup"
    
    # Проверяем, что количество записей увеличилось (или не изменилось, если это обновление)
    if len(faq_data) < original_count:
        logger.error("⚠️ КРИТИЧЕСКАЯ ОШИБКА: Количество записей уменьшилось! "
                   "Было: %d, стало: %d", original_count, len(faq_data))
        return False
    
    # Создаем резервную копию перед сохранением
    try:
        if os.path.exists(FAQ_PATH):
            shutil.copy2(FAQ_PATH, backup_path)
            logger.debug("Создана резервная копия FAQ: %s", backup_path)
    except Exception as e:
        logger.warning("Не удалось создать резервную копию FAQ: %s", e)
    
    # Сохраняем обновленный FAQ атомарно
    try:
        os.makedirs(os.path.dirname(FAQ_PATH), exist_ok=True)
        # Используем временный файл для атомарной записи
        temp_path = f"{FAQ_PATH}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(faq_data, f, ensure_ascii=False, indent=2)
        # Атомарно заменяем файл
        os.replace(temp_path, FAQ_PATH)
        logger.info("✅ FAQ сохранен: %d записей (было: %d)", len(faq_data), original_count)
        return True
    except Exception as e:
        logger.exception("Ошибка при сохранении FAQ: %s", e)
        # Восстанавливаем из резервной копии
        if os.path.exists(backup_path):
            try:
                shutil.copy2(backup_path, FAQ_PATH)
                logger.info("Восстановлен FAQ из резервной копии после ошибки сохранения")
            except Exception as restore_e:
                logger.exception("Не удалось восстановить FAQ из резервной копии: %s", restore_e)
        return False


def _validate_faq_entry(question: str, answer: str) -> tuple[bool, str]:
    """
    Проверяет подходящесть FAQ записи.
    
    Args:
        question: Вопрос
        answer: Ответ
        
    Returns:
        Кортеж (валидна ли запись, сообщение об ошибке)
    """
    question = question.strip() if question else ""
    answer = answer.strip() if answer else ""
    
    if not question:
        return False, "Вопрос не может быть пустым"
    
    if not answer:
        return False, "Ответ не может быть пустым"
    
    # Минимальная длина вопроса (хотя бы 3 символа)
    if len(question) < 3:
        return False, "Вопрос слишком короткий (минимум 3 символа)"
    
    # Минимальная длина ответа (хотя бы 5 символов)
    if len(answer) < 5:
        return False, "Ответ слишком короткий (минимум 5 символов)"
    
    # Максимальная длина вопроса (не более 500 символов)
    if len(question) > 500:
        return False, "Вопрос слишком длинный (максимум 500 символов)"
    
    # Максимальная длина ответа (не более 2000 символов)
    if len(answer) > 2000:
        return False, "Ответ слишком длинный (максимум 2000 символов)"
    
    return True, ""


def _add_faq_entry_safe(question: str, answer: str, source: str) -> tuple[bool, str]:
    """
    Безопасно добавляет одну запись в FAQ с проверкой уникальности и валидацией.
    
    Args:
        question: Вопрос
        answer: Ответ
        source: Источник добавления ("admin", "manager", "user_like", "manager_like")
        
    Returns:
        Кортеж (успешно ли добавлено, сообщение)
    """
    # Валидация записи
    logger.debug("Валидация FAQ записи: вопрос='%s' (длина: %d), ответ='%s' (длина: %d), источник='%s'", 
                 question[:50], len(question), answer[:50], len(answer), source)
    is_valid, error_msg = _validate_faq_entry(question, answer)
    if not is_valid:
        logger.warning("FAQ запись не прошла валидацию: %s (вопрос: '%s', ответ: '%s')", 
                       error_msg, question[:50], answer[:50])
        return False, error_msg
    
    # Загружаем FAQ
    logger.debug("Загрузка FAQ для добавления записи")
    current_faq, original_count = _load_faq_safe()
    logger.debug("Загружен FAQ: %d записей", len(current_faq))
    
    # Проверяем уникальность вопроса (case-insensitive)
    question_lower = question.lower().strip()
    existing_questions = {
        item.get("question", "").lower().strip() 
        for item in current_faq 
        if item.get("question")
    }
    logger.debug("Проверка уникальности: вопрос='%s', существующих вопросов: %d", 
                 question_lower[:50], len(existing_questions))
    
    if question_lower in existing_questions:
        logger.info("Вопрос уже есть в FAQ, пропускаем добавление: '%s'", question[:50])
        return False, "Вопрос уже существует в FAQ"
    
    # Добавляем новую запись
    new_entry = {
        "question": question.strip(),
        "answer": answer.strip(),
        "source": source
    }
    current_faq.append(new_entry)
    
    # Сохраняем с проверкой
    if _save_faq_safe(current_faq, original_count):
        logger.info("✅ FAQ запись добавлена: вопрос='%s', источник='%s', было: %d, стало: %d", 
                   question[:50], source, original_count, len(current_faq))
        return True, f"Запись успешно добавлена в FAQ (всего: {len(current_faq)} записей)"
    else:
        return False, "Ошибка при сохранении FAQ"


def _add_faq_entries_batch(entries: List[Dict[str, str]], source: str) -> tuple[int, int, List[str]]:
    """
    Безопасно добавляет несколько записей в FAQ с проверкой уникальности и валидацией.
    
    Args:
        entries: Список записей [{"question": str, "answer": str}, ...]
        source: Источник добавления ("admin", "manager", "user_like", "manager_like")
        
    Returns:
        Кортеж (количество добавленных, количество пропущенных, список ошибок)
    """
    if not entries:
        return 0, 0, []
    
    # Загружаем FAQ один раз
    current_faq, original_count = _load_faq_safe()
    
    # Собираем существующие вопросы
    existing_questions = {
        item.get("question", "").lower().strip() 
        for item in current_faq 
        if item.get("question")
    }
    
    added_count = 0
    skipped_count = 0
    errors = []
    
    # Обрабатываем каждую запись
    for entry in entries:
        question = entry.get("question", "").strip()
        answer = entry.get("answer", "").strip()
        
        # Валидация
        is_valid, error_msg = _validate_faq_entry(question, answer)
        if not is_valid:
            skipped_count += 1
            errors.append(f"'{question[:30]}...': {error_msg}")
            continue
        
        # Проверка уникальности
        question_lower = question.lower().strip()
        if question_lower in existing_questions:
            skipped_count += 1
            logger.debug("Пропускаем дубликат вопроса: %s", question[:50])
            continue
        
        # Добавляем запись
        new_entry = {
            "question": question,
            "answer": answer,
            "source": source
        }
        current_faq.append(new_entry)
        existing_questions.add(question_lower)
        added_count += 1
    
    # Сохраняем все добавленные записи одним разом
    if added_count > 0:
        if _save_faq_safe(current_faq, original_count):
            logger.info("✅ Добавлено %d записей в FAQ (пропущено: %d), источник='%s', было: %d, стало: %d", 
                       added_count, skipped_count, source, original_count, len(current_faq))
        else:
            return 0, len(entries), ["Ошибка при сохранении FAQ"]
    
    return added_count, skipped_count, errors


def _parse_faq_text(text: str) -> List[Dict[str, str]]:
    """
    Парсит текст FAQ в список вопросов-ответов.
    
    Поддерживает форматы:
    - Q: вопрос\nA: ответ
    - JSON: [{"question": "...", "answer": "..."}]
    
    Args:
        text: Текст для парсинга
        
    Returns:
        Список словарей {"question": str, "answer": str}
    """
    faq_items = []
    
    # Пробуем парсить как JSON
    try:
        # Ищем JSON массив в тексте
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "question" in item and "answer" in item:
                        faq_items.append({
                            "question": str(item["question"]).strip(),
                            "answer": str(item["answer"]).strip()
                        })
                if faq_items:
                    return faq_items
    except (json.JSONDecodeError, ValueError):
        pass
    
    # Парсим формат Q: ... A: ...
    qa_pattern = re.compile(r'(?:Q|Вопрос)[:\s]+(.+?)(?:\n|$)(?:A|Ответ)[:\s]+(.+?)(?=\n(?:Q|Вопрос)|$)', re.IGNORECASE | re.DOTALL)
    matches = qa_pattern.findall(text)
    
    for question, answer in matches:
        question = question.strip()
        answer = answer.strip()
        if question and answer:
            faq_items.append({"question": question, "answer": answer})
    
    # Если не нашли по паттерну, пробуем разделить по строкам
    if not faq_items:
        lines = text.split('\n')
        current_question = None
        current_answer = []
        
        for line in lines:
            line = line.strip()
            if not line:
                if current_question and current_answer:
                    faq_items.append({
                        "question": current_question,
                        "answer": "\n".join(current_answer).strip()
                    })
                    current_question = None
                    current_answer = []
                continue
            
            # Проверяем, начинается ли строка с Q: или Вопрос:
            if re.match(r'^(?:Q|Вопрос)[:\s]+', line, re.IGNORECASE):
                if current_question and current_answer:
                    faq_items.append({
                        "question": current_question,
                        "answer": "\n".join(current_answer).strip()
                    })
                current_question = re.sub(r'^(?:Q|Вопрос)[:\s]+', '', line, flags=re.IGNORECASE).strip()
                current_answer = []
            # Проверяем, начинается ли строка с A: или Ответ:
            elif re.match(r'^(?:A|Ответ)[:\s]+', line, re.IGNORECASE):
                answer_text = re.sub(r'^(?:A|Ответ)[:\s]+', '', line, flags=re.IGNORECASE).strip()
                if current_question:
                    current_answer.append(answer_text)
                else:
                    # Если нет вопроса, создаем пустой вопрос
                    current_question = ""
                    current_answer.append(answer_text)
            elif current_question is not None:
                # Продолжение ответа
                current_answer.append(line)
            else:
                # Если нет вопроса, это может быть первый вопрос
                current_question = line
        
        # Добавляем последний вопрос-ответ
        if current_question and current_answer:
            faq_items.append({
                "question": current_question,
                "answer": "\n".join(current_answer).strip()
            })
    
    return faq_items


# Обработчик для добавления FAQ текстом (нарастающим итогом)
@user_router.message(AdminStates.waiting_for_faq_text)
async def handle_faq_text(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает добавление FAQ текстом (нарастающим итогом).
    
    Парсит текст напрямую без использования LLM.
    Поддерживает форматы: Q: ... A: ... или JSON.
    
    Args:
        message: Сообщение с текстом FAQ
        state: FSM контекст для управления состоянием
    """
    # Проверяем, не является ли сообщение командой
    if message.text and message.text.startswith("/"):
        await state.clear()
        logger.info("Команда %s отменила ожидание FAQ текста", message.text.split()[0])
        return
    
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение с FAQ или /cancel для отмены.")
        return
    
    try:
        new_text = message.text.strip()
        
        if not new_text:
            await message.answer("❌ Текст не может быть пустым. Отправьте текст или /cancel для отмены.")
            return
        
        # Загружаем существующий FAQ с защитой от потери данных
        original_faq_count = 0
        backup_path = f"{FAQ_PATH}.backup"
        try:
            with open(FAQ_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    logger.warning("FAQ файл пуст, пробуем восстановить из резервной копии")
                    raise ValueError("FAQ файл пуст")
                current_faq = json.loads(content)
            # Проверяем, что это список
            if not isinstance(current_faq, list):
                logger.error("FAQ файл не содержит список, пробуем восстановить из резервной копии")
                raise ValueError("FAQ файл не содержит список")
            else:
                original_faq_count = len(current_faq)
                logger.info("Загружен FAQ для добавления текстом: %d записей", original_faq_count)
        except FileNotFoundError:
            logger.warning("FAQ файл не найден, создаем новый")
            current_faq = []
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Ошибка при загрузке FAQ файла: %s, пробуем восстановить из резервной копии", e)
            # Пробуем исправить JSON (убрать лишние запятые в конце)
            try:
                with open(FAQ_PATH, "r", encoding="utf-8") as f:
                    content = f.read()
                # Убираем лишние запятые перед закрывающими скобками
                content = re.sub(r',\s*\]', ']', content)
                content = re.sub(r',\s*\}', '}', content)
                current_faq = json.loads(content)
                if isinstance(current_faq, list) and len(current_faq) > 0:
                    original_faq_count = len(current_faq)
                    logger.info("✅ FAQ исправлен автоматически: %d записей", original_faq_count)
                    # Сохраняем исправленный FAQ
                    with open(FAQ_PATH, "w", encoding="utf-8") as f:
                        json.dump(current_faq, f, ensure_ascii=False, indent=2)
                else:
                    current_faq = []
            except Exception as fix_e:
                logger.warning("Не удалось автоматически исправить FAQ: %s", fix_e)
                current_faq = []
        except Exception as e:
            logger.exception("Неожиданная ошибка при загрузке FAQ: %s", e)
            current_faq = []
        
        # Если не удалось загрузить FAQ, пробуем восстановить из резервной копии
        if not isinstance(current_faq, list):
            current_faq = []
        
        if isinstance(current_faq, list) and len(current_faq) == 0 and os.path.exists(backup_path):
            try:
                logger.info("Восстанавливаю FAQ из резервной копии: %s", backup_path)
                with open(backup_path, "r", encoding="utf-8") as f:
                    current_faq = json.load(f)
                if isinstance(current_faq, list):
                    original_faq_count = len(current_faq)
                    logger.info("✅ FAQ восстановлен из резервной копии: %d записей", original_faq_count)
                    # Восстанавливаем основной файл из резервной копии
                    shutil.copy2(backup_path, FAQ_PATH)
                else:
                    current_faq = []
            except Exception as restore_e:
                logger.exception("Не удалось восстановить FAQ из резервной копии: %s", restore_e)
                current_faq = []
        
        # Всегда используем LLM для красивого парсинга FAQ
        logger.info("Обрабатываю FAQ текст через LLM для структурирования")
        await message.answer("🤖 Обрабатываю текст с помощью LLM для создания структуры FAQ...")
        
        try:
            # Обрабатываем текст через LLM для структурирования
            prompt = f"""
Ты — эксперт по структурированию данных для FAQ базы знаний.

Вот текст для добавления в FAQ:

{new_text}

Твоя задача: извлечь из текста все вопросы и ответы и структурировать их в JSON массив.

ВАЖНЫЕ ПРАВИЛА:
1. Создай массив JSON вида: [{{"question": "...", "answer": "..."}}]
2. Если текст содержит формат Q: ... A: ... или Вопрос: ... Ответ: ... - извлеки ВСЕ такие пары
3. Если текст содержит JSON формат - извлеки его КАК ЕСТЬ, без изменений
4. Если текст свободный БЕЗ явных вопросов (нет Q:, Вопрос:, JSON) - верни пустой массив []
5. НЕ создавай вопросы самостоятельно, если их нет в тексте
6. НЕ дублируй одинаковые вопросы - если вопрос повторяется, оставь только один
7. Каждый вопрос должен быть уникальным
8. Ответ должен быть полным и информативным, как указано в тексте

ПРИМЕРЫ:
- Текст: "Q: Как оформить визу? A: Нужно собрать документы" → [{{"question": "Как оформить визу?", "answer": "Нужно собрать документы"}}]
- Текст: "Q: вопрос1? A: ответ1\nQ: вопрос2? A: ответ2" → [{{"question": "вопрос1?", "answer": "ответ1"}}, {{"question": "вопрос2?", "answer": "ответ2"}}]
- Текст: "[{{\"question\": \"...\", \"answer\": \"...\"}}]" → извлеки JSON как есть
- Текст: "Просто информация без вопросов" → []

Отвечай ТОЛЬКО валидный JSON массив - без дополнительного текста, без комментариев, без markdown разметки.
Если в тексте нет явных вопросов (Q:, Вопрос:, JSON), верни пустой массив: []
"""
            
            use_temperature = LLM_MODEL not in ["gpt-5-mini", "gpt-5"]
            
            if use_temperature:
                response = await client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,  # Низкая температура для более точного парсинга
                )
            else:
                response = await client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                )
            llm_response = response.choices[0].message.content.strip()
            
            logger.debug("LLM ответ для FAQ: %s", llm_response[:500])
            
            # Очищаем ответ от markdown разметки, если есть
            llm_response = re.sub(r'```json\s*', '', llm_response)
            llm_response = re.sub(r'```\s*', '', llm_response)
            llm_response = llm_response.strip()
            
            # Извлекаем JSON из ответа
            match = re.search(r'\[.*\]', llm_response, re.DOTALL)
            if match:
                try:
                    parsed_faq = json.loads(match.group(0))
                    if isinstance(parsed_faq, list):
                        new_faq = []
                        seen_questions = set()
                        
                        for item in parsed_faq:
                            if not isinstance(item, dict):
                                continue
                            
                            question = str(item.get("question", "")).strip()
                            answer = str(item.get("answer", "")).strip()
                            
                            # Очищаем от лишних символов и JSON-разметки
                            question = re.sub(r'^["\']|["\']$', '', question)  # Убираем кавычки в начале/конце
                            question = re.sub(r'^question["\']?\s*:\s*["\']?', '', question, flags=re.IGNORECASE)  # Убираем "question": "
                            question = re.sub(r'["\']?\s*,\s*$', '', question)  # Убираем запятую в конце
                            question = question.strip()
                            
                            answer = re.sub(r'^["\']|["\']$', '', answer)  # Убираем кавычки в начале/конце
                            answer = re.sub(r'^answer["\']?\s*:\s*["\']?', '', answer, flags=re.IGNORECASE)  # Убираем "answer": "
                            answer = re.sub(r'["\']?\s*\}\s*,\s*\{', '', answer)  # Убираем }, {
                            answer = re.sub(r'["\']?\s*\}\s*$', '', answer)  # Убираем } в конце
                            answer = answer.strip()
                            
                            # Проверяем, что есть и вопрос, и ответ, и они не содержат JSON-разметку
                            if not question or not answer:
                                continue
                            
                            # Проверяем, что вопрос и ответ не содержат явную JSON-разметку
                            if re.search(r'["\']?\s*question["\']?\s*:', question, re.IGNORECASE):
                                logger.warning("Пропускаем вопрос с JSON-разметкой: %s", question[:50])
                                continue
                            if re.search(r'["\']?\s*answer["\']?\s*:', answer, re.IGNORECASE):
                                logger.warning("Пропускаем ответ с JSON-разметкой: %s", answer[:50])
                                continue
                            
                            # Проверяем на дубликаты (по нижнему регистру)
                            question_lower = question.lower().strip()
                            if question_lower in seen_questions:
                                logger.debug("Пропускаем дубликат вопроса: %s", question[:50])
                                continue
                            
                            seen_questions.add(question_lower)
                            new_faq.append({
                                "question": question,
                                "answer": answer,
                                "source": "admin"  # Добавлено админом через команду
                            })
                        
                        logger.info("LLM успешно распарсил FAQ: %d уникальных записей", len(new_faq))
                    else:
                        new_faq = []
                except json.JSONDecodeError as e:
                    logger.error("Ошибка парсинга JSON от LLM: %s, ответ: %s", e, llm_response[:500])
                    new_faq = []
            else:
                logger.warning("LLM не вернул JSON массив, ответ: %s", llm_response[:500])
                new_faq = []
            
            # Если LLM не вернул результат, пробуем прямой парсинг для формата Q: ... A: ...
            if not new_faq:
                logger.info("LLM не вернул результат, пробуем прямой парсинг для формата Q: ... A: ...")
                parsed_faq = _parse_faq_text(new_text)
                
                if parsed_faq:
                    # Добавляем source для всех записей
                    new_faq = [
                        {**item, "source": "admin"}  # Добавлено админом через команду
                        for item in parsed_faq
                    ]
                    logger.info("Прямой парсинг успешно извлек %d записей", len(new_faq))
                else:
                    await message.answer(
                        "❌ Не удалось создать структуру FAQ из текста.\n\n"
                        "Попробуйте:\n"
                        "• Использовать структурированный формат: Q: вопрос\nA: ответ\n\n"
                        "• Или JSON: [{\"question\": \"...\", \"answer\": \"...\"}]\n\n"
                        "💡 Для отмены отправьте /cancel"
                    )
                    return
        except Exception as e:
            logger.exception("Ошибка LLM при обработке FAQ текста: %s", e)
            await message.answer(
                f"❌ Ошибка при обработке текста через LLM: {e}\n\n"
                "Попробуйте использовать структурированный формат:\n"
                "Q: вопрос\nA: ответ\n\n"
                "💡 Для отмены отправьте /cancel"
            )
            return
        
        # Используем единую функцию добавления FAQ с проверкой уникальности и валидацией
        added_count, skipped_count, errors = _add_faq_entries_batch(new_faq, "admin")
        
        if not added_count:
            await message.answer(
                "⚠️ Не найдено новых уникальных вопросов для добавления.\n\n"
                "Возможные причины:\n"
                "• В тексте нет явных вопросов\n"
                "• Все вопросы уже есть в FAQ\n"
                "• Текст не содержит структурированных вопросов-ответов"
            )
            await state.clear()
            return
        
        logger.info("✅ FAQ обновлен текстом пользователем %d, добавлено %d уникальных записей (пропущено: %d)", 
                   message.from_user.id, added_count, skipped_count)
        success_msg = f"✅ FAQ обновлен. Добавлено {added_count} уникальных записей"
        if skipped_count > 0:
            success_msg += f" (пропущено: {skipped_count})"
        await message.answer(success_msg)
    except Exception as e:
        logger.exception("Ошибка при добавлении FAQ текстом: %s", e)
        await message.answer(f"❌ Ошибка при добавлении FAQ: {e}")
    finally:
        await state.clear()


# ----------------------------
# Динамический контекст
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
        "📌 <b>Динамический контекст</b> — то, что часто меняется:\n"
        "• Услуги и их стоимости\n"
        "• Сроки выдачи виз\n"
        "• Актуальные тарифы\n"
        "• Другая часто обновляемая информация\n\n"
        "Выберите действие:",
        reply_markup=keyboard
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
        "📌 <b>Динамический контекст</b> — то, что часто меняется:\n"
        "• Услуги и их стоимости\n"
        "• Сроки выдачи виз\n"
        "• Актуальные тарифы\n\n"
        "💡 <b>Можно отправлять частями</b> — каждое сообщение будет добавлено к предыдущему.\n"
        "💡 Для завершения ввода отправьте /done\n"
        "💡 Для отмены отправьте /cancel"
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
        os.makedirs(os.path.dirname(DYNAMIC_CONTEXT_PATH), exist_ok=True)
        with open(DYNAMIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
            f.write(accumulated_text)
        
        logger.info("✅ Динамический контекст обновлен пользователем %d, длина: %d символов", 
                   message.from_user.id, len(accumulated_text))
        await message.answer(f"✅ Динамический контекст обновлён. Длина: {len(accumulated_text)} символов.")
    except Exception as e:
        logger.exception("Ошибка при сохранении динамического контекста: %s", e)
        await message.answer(f"❌ Ошибка при сохранении: {e}")
    finally:
        await state.clear()


@user_router.message(AdminStates.waiting_for_dynamic_context)
async def handle_dynamic_context(message: Message, state: FSMContext) -> None:
    """Обрабатывает текст динамического контекста (поддерживает ввод частями)."""
    # Проверяем, не является ли сообщение командой
    if message.text and message.text.startswith("/"):
        await state.clear()
        logger.info("Команда %s отменила ожидание динамического контекста", message.text.split()[0])
        return
    
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение с контекстом, /done для завершения или /cancel для отмены.")
        return
    
    try:
        new_text = message.text.strip()
        
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
# Профиль цифрового ассистента
# ----------------------------
@user_router.message(F.text.regexp(r"^/systemprompt\b"))
async def cmd_system_prompt_menu(message: Message, state: FSMContext) -> None:
    """Показывает меню управления профилем цифрового ассистента."""
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    await state.clear()
    
    # Создаем inline кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👁️ Просмотреть", callback_data="prompt_view"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="prompt_edit"),
        ],
    ])
    
    await message.answer(
        "🤖 Управление профилем цифрового ассистента\n\n"
        "📌 <b>Профиль цифрового ассистента</b> — то, как ведет себя ассистент:\n"
        "• Его характер\n"
        "• Манеры общения\n"
        "• Правила поведения\n"
        "• Стиль ответов\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )


@user_router.callback_query(F.data == "prompt_view")
async def callback_prompt_view(callback: CallbackQuery) -> None:
    """Обработчик кнопки 'Просмотреть профиль цифрового ассистента'."""
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
                    await callback.message.answer(f"🤖 Текущий профиль цифрового ассистента:\n\n{content}")
                else:
                    # Разбиваем на части
                    parts = [content[i:i+3500] for i in range(0, len(content), 3500)]
                    for i, part in enumerate(parts, 1):
                        if i == 1:
                            await callback.message.answer(f"🤖 Текущий профиль цифрового ассистента (часть {i}/{len(parts)}):\n\n{part}")
                        else:
                            await callback.message.answer(f"🤖 Продолжение профиля цифрового ассистента (часть {i}/{len(parts)}):\n\n{part}")
            else:
                await callback.message.answer("🤖 Профиль цифрового ассистента пуст.")
        else:
            await callback.message.answer("🤖 Профиль цифрового ассистента не установлен (используется значение по умолчанию).")
    except Exception as e:
        logger.exception("Ошибка при просмотре профиля цифрового ассистента: %s", e)
        await callback.message.answer(f"❌ Ошибка при просмотре: {e}")


@user_router.callback_query(F.data == "prompt_edit")
async def callback_prompt_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Изменить профиль цифрового ассистента'."""
    await callback.answer()
    await state.clear()
    await state.update_data(accumulated_text="")
    await callback.message.answer(
        "📝 Отправьте новый текст профиля цифрового ассистента (он перезапишет старый).\n\n"
        "📌 <b>Профиль цифрового ассистента</b> — то, как ведет себя ассистент:\n"
        "• Его характер\n"
        "• Манеры общения\n"
        "• Правила поведения\n"
        "• Стиль ответов\n\n"
        "💡 <b>Можно отправлять частями</b> — каждое сообщение будет добавлено к предыдущему.\n"
        "💡 Для завершения ввода отправьте /done\n"
        "💡 Для отмены отправьте /cancel"
    )
    await state.set_state(AdminStates.waiting_for_system_prompt)


@user_router.message(AdminStates.waiting_for_system_prompt, F.text.regexp(r"^/cancel\b"))
async def handle_system_prompt_cancel(message: Message, state: FSMContext) -> None:
    """Отменяет установку профиля цифрового ассистента."""
    await state.clear()
    await message.answer("✅ Установка профиля цифрового ассистента отменена.")


@user_router.message(AdminStates.waiting_for_system_prompt, F.text.regexp(r"^/done\b"))
async def handle_system_prompt_done(message: Message, state: FSMContext) -> None:
    """Завершает ввод профиля цифрового ассистента и сохраняет его."""
    data = await state.get_data()
    accumulated_text = data.get("accumulated_text", "").strip()
    
    if not accumulated_text:
        await message.answer("❌ Профиль пуст. Отправьте текст или /cancel для отмены.")
        return
    
    try:
        # Сохраняем профиль
        os.makedirs(os.path.dirname(SYSTEM_PROMPT_PATH), exist_ok=True)
        with open(SYSTEM_PROMPT_PATH, "w", encoding="utf-8") as f:
            f.write(accumulated_text)
        
        logger.info("✅ Профиль цифрового ассистента обновлен пользователем %d, длина: %d символов", 
                   message.from_user.id, len(accumulated_text))
        await message.answer(f"✅ Профиль цифрового ассистента обновлён. Длина: {len(accumulated_text)} символов.")
    except Exception as e:
        logger.exception("Ошибка при сохранении профиля цифрового ассистента: %s", e)
        await message.answer(f"❌ Ошибка при сохранении: {e}")
    finally:
        await state.clear()


@user_router.message(AdminStates.waiting_for_system_prompt)
async def handle_system_prompt(message: Message, state: FSMContext) -> None:
    """Обрабатывает текст профиля цифрового ассистента (поддерживает ввод частями)."""
    # Проверяем, не является ли сообщение командой
    if message.text and message.text.startswith("/"):
        await state.clear()
        logger.info("Команда %s отменила ожидание профиля цифрового ассистента", message.text.split()[0])
        return
    
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение с профилем, /done для завершения или /cancel для отмены.")
        return
    
    try:
        new_text = message.text.strip()
        
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
        logger.exception("Ошибка при обработке профиля цифрового ассистента: %s", e)
        await message.answer(f"❌ Ошибка при обработке: {e}")


# ----------------------------
# Ответ пользователю в ТГ (через единый responder)
# ----------------------------
@user_router.message(F.text)
async def handle_user_message(message: Message) -> None:
    """
    Обрабатывает обычные текстовые сообщения от пользователей.
    
    Генерирует ответ через LLM на основе FAQ, истории диалога и статического контекста.
    
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
    
    # Создаем кнопки для рейтинга
    qa_id = f"{int(time.time() * 1000)}{random.randint(1000, 9999)}"
    TEMP_QA[qa_id] = {"question": user_question, "answer": answer}
    
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍", callback_data=f"rate_up|{qa_id}"),
                InlineKeyboardButton(text="👎", callback_data=f"rate_down|{qa_id}"),
            ]
        ]
    )
    
    try:
        await message.reply(answer, reply_markup=markup)
        logger.info("✅ Ответ успешно отправлен в Telegram для dialog_id=%s", dialog_id)
        
        # Сохраняем ответ в историю ТОЛЬКО после успешной отправки
        try:
            from responder import _load_json, _save_json, CHAT_HISTORY_PATH
            chat_history = _load_json(CHAT_HISTORY_PATH, {})
            dialog_history = chat_history.get(dialog_id, [])
            
            # Добавляем ответ ассистента в историю с временной меткой
            dialog_history.append({
                "role": "assistant",
                "content": answer,
                "timestamp": datetime.now().isoformat()
            })
            # Сохраняем всю историю (без ограничений)
            chat_history[dialog_id] = dialog_history
            _save_json(CHAT_HISTORY_PATH, chat_history)
            
            logger.info(
                "Saved chat history for dialog_id=%s: %d messages (after successful send)",
                dialog_id, len(chat_history[dialog_id])
            )
        except Exception as e:
            logger.warning("Failed to save chat history after sending to Telegram: %s", e)
    except Exception as e:
        logger.exception("Ошибка при отправке ответа в Telegram: %s", e)
        await message.answer("Извините, произошла ошибка при отправке ответа.")


# ----------------------------
# Обработка обратной связи (👍 👎)
# ----------------------------
@user_router.callback_query(F.data.startswith("rate_"))
async def handle_rating(callback: CallbackQuery) -> None:
    """
    Обрабатывает обратную связь от пользователей (👍 👎).
    
    При положительном отзыве (👍) добавляет вопрос-ответ в FAQ.
    При отрицательном отзыве (👎) уведомляет о необходимости передачи менеджеру.
    
    Args:
        callback: Callback query с данными рейтинга
    """
    if not callback.data:
        return
    
    try:
        parts = callback.data.split("|")
        if len(parts) != 2:
            logger.warning("Invalid callback data format: %s", callback.data)
            await callback.answer("Ошибка обработки обратной связи.")
            return
        
        action, qa_id = parts
        qa_data = TEMP_QA.get(qa_id)

        if not qa_data:
            await callback.answer("Сообщение устарело, но кнопки останутся")
            return

        if action == "rate_up":
            # Загружаем существующий FAQ
            original_faq_count = 0
            backup_path = f"{FAQ_PATH}.backup"
            
            try:
                with open(FAQ_PATH, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if not content:
                        logger.warning("FAQ файл пуст, пробуем восстановить из резервной копии")
                        raise ValueError("FAQ файл пуст")
                    faq_data = json.loads(content)
                # Проверяем, что это список
                if not isinstance(faq_data, list):
                    logger.error("FAQ файл не содержит список, пробуем восстановить из резервной копии")
                    raise ValueError("FAQ файл не содержит список")
                else:
                    original_faq_count = len(faq_data)
                    if original_faq_count == 0:
                        logger.warning("FAQ файл содержит пустой список, пробуем восстановить из резервной копии")
                        raise ValueError("FAQ файл содержит пустой список")
                logger.info("Загружен FAQ: %d записей", original_faq_count)
            except FileNotFoundError:
                logger.warning("FAQ файл не найден, пробуем восстановить из резервной копии")
                faq_data = None
            except (json.JSONDecodeError, ValueError) as e:
                logger.error("Ошибка при загрузке FAQ файла: %s, пробуем восстановить из резервной копии", e)
                # Пробуем исправить JSON (убрать лишние запятые в конце)
                try:
                    with open(FAQ_PATH, "r", encoding="utf-8") as f:
                        content = f.read()
                    # Убираем лишние запятые перед закрывающими скобками
                    content = re.sub(r',\s*\]', ']', content)
                    content = re.sub(r',\s*\}', '}', content)
                    faq_data = json.loads(content)
                    if isinstance(faq_data, list) and len(faq_data) > 0:
                        original_faq_count = len(faq_data)
                        logger.info("✅ FAQ исправлен автоматически: %d записей", original_faq_count)
                        # Сохраняем исправленный FAQ
                        with open(FAQ_PATH, "w", encoding="utf-8") as f:
                            json.dump(faq_data, f, ensure_ascii=False, indent=2)
                    else:
                        faq_data = None
                except Exception as fix_e:
                    logger.warning("Не удалось автоматически исправить FAQ: %s", fix_e)
                    faq_data = None
            except Exception as e:
                logger.exception("Неожиданная ошибка при загрузке FAQ: %s", e)
                faq_data = None
            
            # Если не удалось загрузить FAQ, пробуем восстановить из резервной копии
            if faq_data is None:
                if os.path.exists(backup_path):
                    try:
                        logger.info("Восстанавливаю FAQ из резервной копии: %s", backup_path)
                        with open(backup_path, "r", encoding="utf-8") as f:
                            faq_data = json.load(f)
                        if isinstance(faq_data, list):
                            original_faq_count = len(faq_data)
                            logger.info("✅ FAQ восстановлен из резервной копии: %d записей", original_faq_count)
                            # Восстанавливаем основной файл из резервной копии
                            shutil.copy2(backup_path, FAQ_PATH)
                        else:
                            logger.error("Резервная копия FAQ не содержит список")
                            faq_data = []
                    except Exception as restore_e:
                        logger.exception("Не удалось восстановить FAQ из резервной копии: %s", restore_e)
                        faq_data = []
                else:
                    logger.warning("Резервная копия FAQ не найдена, создаем новый FAQ")
                    faq_data = []
            
            # Проверяем, нет ли уже такого вопроса (сравниваем по нижнему регистру)
            question = qa_data.get("question", "").strip()
            question_lower = question.lower() if question else ""
            
            if not question:
                logger.warning("Пустой вопрос в qa_data, пропускаем добавление")
                await callback.answer("Ошибка: пустой вопрос.")
                return
            
            # Используем единую функцию добавления FAQ с проверкой уникальности и валидацией
            success, message = _add_faq_entry_safe(question, qa_data.get("answer", "").strip(), "user_like")
            
            if success:
                await callback.answer("Ответ добавлен в базу знаний.")
            else:
                if "уже существует" in message.lower() or "уже есть" in message.lower():
                    await callback.answer("Такой вопрос уже есть в базе знаний.")
                else:
                    logger.debug("Не удалось добавить FAQ через лайк: %s", message)
                    await callback.answer("Не удалось добавить в базу знаний.")
        else:  # rate_down
            await callback.answer("Спасибо, передадим менеджеру.")
            
    except Exception as e:
        logger.exception("Ошибка при обработке оценки: %s", e)
        await callback.answer("Ошибка при обработке оценки.")
