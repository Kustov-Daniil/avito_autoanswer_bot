"""
Основной модуль приложения.

Содержит Flask webhook для обработки сообщений от Avito,
обработчики для Telegram бота и логику интеграции между сервисами.
"""
import asyncio
import threading
import logging
import re
import json
from datetime import datetime
from typing import Dict, Any, Optional, Callable, Awaitable, List
from flask import Flask, request, jsonify, Response
from aiogram import F
from aiogram.types import Message

from create_bot import bot, dp
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from config import (
    TELEGRAM_MANAGER_ID, TELEGRAM_BOT_TOKEN,
    AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_ACCOUNT_ID,
    SIGNAL_PHRASES
)
from avito_api import send_message, list_messages_v3
from avito_sessions import can_bot_reply, set_waiting_manager, set_cooldown_after_manager
from responder import generate_reply
from user_bot import user_router
from telegram_utils import safe_send_message, safe_send_message_to_chat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Константы для webhook обработки
WEBHOOK_ENDPOINT: str = "/avito/webhook"
HEALTH_ENDPOINT: str = "/health"
FLASK_HOST: str = "0.0.0.0"
FLASK_PORT: int = 8080

# Регулярные выражения для извлечения данных
CHAT_ID_PATTERN_HTML: re.Pattern = re.compile(r"Avito Chat ID:\s*<code>(.*?)</code>|<code>([0-9a-zA-Z:_-]+)</code>")
CHAT_ID_PATTERN_TEXT: re.Pattern = re.compile(r"Avito Chat ID:\s*([0-9a-zA-Z:_-]+)|([0-9a-zA-Z:_-]+)$")
AVITO_CHAT_ID_PATTERN: re.Pattern = re.compile(r"(?i)Avito Chat ID[:\s]*([0-9a-zA-Z:_-]+)|<code>([0-9a-zA-Z:_-]+)</code>|([0-9a-zA-Z:_-]+)$")


def check_config() -> bool:
    """
    Проверяет наличие всех необходимых переменных окружения.
    
    Returns:
        True если все переменные установлены, False иначе
    """
    missing: list[str] = []
    
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_MANAGER_ID:
        missing.append("TELEGRAM_MANAGER_ID")
    if not AVITO_CLIENT_ID:
        missing.append("AVITO_CLIENT_ID")
    if not AVITO_CLIENT_SECRET:
        missing.append("AVITO_CLIENT_SECRET")
    if not AVITO_ACCOUNT_ID:
        missing.append("AVITO_ACCOUNT_ID")
    
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        logger.error("Please set these variables in your .env file or environment")
        return False
    
    logger.info("Configuration check passed:")
    logger.info("  TELEGRAM_BOT_TOKEN: %s", "✓" if TELEGRAM_BOT_TOKEN else "✗")
    logger.info("  TELEGRAM_MANAGER_ID: %s", TELEGRAM_MANAGER_ID)
    logger.info("  AVITO_CLIENT_ID: %s", "✓" if AVITO_CLIENT_ID else "✗")
    logger.info("  AVITO_CLIENT_SECRET: %s", "✓" if AVITO_CLIENT_SECRET else "✗")
    logger.info("  AVITO_ACCOUNT_ID: %s", AVITO_ACCOUNT_ID if AVITO_ACCOUNT_ID else "✗ NOT SET!")
    
    return True


# Проверяем конфигурацию при импорте
if not check_config():
    logger.warning("Some configuration variables are missing. The bot may not work correctly.")

app = Flask(__name__)

# Регистрируем router для команд и ответов в Telegram
dp.include_router(user_router)


def run_async_in_thread(coro: Awaitable[Any]) -> None:
    """
    Запускает async функцию в отдельном потоке с собственным event loop.
    
    Это необходимо для работы async функций из Flask webhook, так как
    Flask работает в синхронном контексте, а aiogram требует async event loop.
    
    Args:
        coro: Async корутина для выполнения
    """
    def run_in_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Создаём wrapper который создаёт задачу внутри запущенного loop
            async def wrapper() -> Any:
                # Создаём задачу внутри запущенного loop - это важно для aiohttp
                task = asyncio.create_task(coro)
                return await task
            
            # run_until_complete создаст wrapper как задачу,
            # и внутри wrapper будет создана задача для coro
            loop.run_until_complete(wrapper())
        except Exception as e:
            logger.exception("Error in async task: %s", e)
        finally:
            # Очищаем все незавершённые задачи
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception as e:
                logger.warning("Error cleaning up tasks: %s", e)
            finally:
                loop.close()
    
    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()


def format_manager_text_with_history(
    chat_id: str,
    current_message: str,
    history: List[Dict[str, Any]],
    chat_info: Optional[Dict[str, Any]] = None,
    user_name: Optional[str] = None
) -> str:
    """
    Форматирует текст уведомления для менеджера в Telegram с историей переписки.
    
    Args:
        chat_id: ID чата в Avito
        current_message: Текущее сообщение от клиента
        history: История сообщений из Avito
        chat_info: Информация о чате (объявление, аккаунт, собеседник, локация)
        user_name: Имя клиента
        
    Returns:
        Отформатированный текст уведомления с историей
    """
    # Извлекаем имя клиента из chat_info или используем переданное
    client_name = user_name or "Клиент"
    if chat_info:
        # Пробуем извлечь имя из разных мест в структуре
        user_info = chat_info.get("user") or chat_info.get("interlocutor") or {}
        if isinstance(user_info, dict):
            client_name = (
                user_info.get("name") or
                user_info.get("first_name") or
                user_info.get("full_name") or
                client_name
            )
        elif isinstance(user_info, str):
            client_name = user_info
    
    # Форматируем текущее сообщение
    header = f"{client_name}: {current_message}"
    
    # Форматируем историю сообщений
    history_lines = []
    if history:
        # Проверяем, если history это список или словарь с вложенным списком
        if isinstance(history, dict):
            messages_list = history.get("messages") or history.get("items") or history.get("value", {}).get("messages") or []
        elif isinstance(history, list):
            messages_list = history
        else:
            messages_list = []
        
        for msg in reversed(messages_list[-20:]):  # Последние 20 сообщений
            if not isinstance(msg, dict):
                continue
            
            # Извлекаем текст сообщения - пробуем разные форматы
            msg_text = ""
            content = msg.get("content") or msg.get("value", {}).get("content") or {}
            if isinstance(content, dict):
                msg_text = (
                    content.get("text") or 
                    content.get("value") or 
                    content.get("message") or
                    ""
                )
            elif isinstance(content, str):
                msg_text = content
            else:
                msg_text = (
                    msg.get("text") or 
                    msg.get("value") or 
                    msg.get("message") or
                    str(content) if content else ""
                )
            
            if not msg_text or not msg_text.strip():
                continue
            
            # Извлекаем дату и время - пробуем разные пути
            created = (
                msg.get("created") or 
                msg.get("created_at") or 
                msg.get("timestamp") or
                msg.get("value", {}).get("created") or
                msg.get("value", {}).get("created_at") or
                None
            )
            date_str = ""
            if created:
                try:
                    # Пробуем разные форматы даты
                    if isinstance(created, (int, float)):
                        dt = datetime.fromtimestamp(created)
                    elif isinstance(created, str):
                        # Пробуем разные форматы строк
                        for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"]:
                            try:
                                dt = datetime.strptime(created.split("+")[0].split(".")[0], fmt)
                                break
                            except ValueError:
                                continue
                        else:
                            dt = datetime.now()
                    else:
                        dt = datetime.now()
                    date_str = dt.strftime("%d.%m %H:%M")
                except Exception:
                    date_str = datetime.now().strftime("%d.%m %H:%M")
            
            # Извлекаем направление и определяем отправителя - пробуем разные пути
            direction = (
                msg.get("direction") or 
                msg.get("value", {}).get("direction") or
                "unknown"
            )
            type_msg = (
                msg.get("type") or 
                msg.get("message_type") or
                msg.get("value", {}).get("type") or
                ""
            )
            
            # Определяем отправителя
            if type_msg and "system" in type_msg.lower():
                # Системные сообщения от Avito
                sender = "Системное: [Системное сообщение]"
            elif direction == "in":
                # Входящее сообщение от клиента
                sender = client_name
            elif direction == "out":
                # Исходящее сообщение от нас (бота/аккаунта)
                # Извлекаем имя аккаунта из chat_info
                account_name = "Visa Way Pro"  # По умолчанию
                if chat_info:
                    account = chat_info.get("account") or {}
                    if isinstance(account, dict):
                        account_name = account.get("name") or account.get("title") or account_name
                    elif isinstance(account, str):
                        account_name = account
                sender = account_name
            else:
                # Неизвестное направление - считаем системным
                sender = "Системное"
            
            # Форматируем строку истории
            if date_str:
                history_lines.append(f"{date_str} {sender}: {msg_text}")
            else:
                history_lines.append(f"{sender}: {msg_text}")
    
    history_text = "\n".join(history_lines) if history_lines else ""
    
    # Форматируем информацию о чате
    chat_details = []
    
    # Информация об объявлении
    if chat_info:
        # Пробуем разные пути к данным об объявлении
        item = (
            chat_info.get("item") or 
            chat_info.get("advertisement") or 
            chat_info.get("ad") or
            chat_info.get("value", {}).get("item") or
            {}
        )
        if isinstance(item, dict):
            title = (
                item.get("title") or 
                item.get("name") or 
                item.get("value", {}).get("title") or
                ""
            )
            price = (
                item.get("price") or 
                item.get("price_value") or 
                item.get("value", {}).get("price") or
                ""
            )
            item_id = (
                item.get("id") or 
                item.get("item_id") or 
                item.get("value", {}).get("id") or
                ""
            )
            if title:
                price_str = f" ({price} ₽)" if price else ""
                item_id_str = f" [#adv{item_id}]" if item_id else ""
                chat_details.append(f"{title}{price_str}{item_id_str}")
        
        # Информация об аккаунте
        account = (
            chat_info.get("account") or 
            chat_info.get("account_info") or
            chat_info.get("value", {}).get("account") or
            {}
        )
        if isinstance(account, dict):
            acc_name = account.get("name") or account.get("title") or account.get("profile_name") or ""
            acc_email = account.get("email") or ""
            acc_phone = account.get("phone") or account.get("phone_number") or ""
            acc_id = account.get("id") or account.get("account_id") or (AVITO_ACCOUNT_ID if AVITO_ACCOUNT_ID else "") or ""
            if acc_name:
                parts = [acc_name]
                if acc_email:
                    parts.append(acc_email)
                if acc_phone:
                    parts.append(acc_phone)
                acc_id_str = f" [#acc{acc_id}]" if acc_id else ""
                chat_details.append(f"Аккаунт: {' '.join(parts)}{acc_id_str}")
        
        # Информация о собеседнике
        user_info = (
            chat_info.get("user") or 
            chat_info.get("interlocutor") or 
            chat_info.get("interlocutor_info") or
            chat_info.get("value", {}).get("user") or
            chat_info.get("value", {}).get("interlocutor") or
            {}
        )
        if isinstance(user_info, dict):
            user_name_full = (
                user_info.get("name") or 
                user_info.get("full_name") or 
                user_info.get("first_name") or
                user_info.get("profile_name") or
                user_info.get("username") or
                client_name
            )
            user_id = (
                user_info.get("id") or 
                user_info.get("user_id") or 
                user_info.get("profile_id") or
                ""
            )
            if user_name_full:
                user_id_str = f" [#user{user_id}]" if user_id else ""
                chat_details.append(f"Собеседник: {user_name_full}{user_id_str}")
        
        # Локация
        location = (
            chat_info.get("location") or 
            chat_info.get("city") or
            chat_info.get("value", {}).get("location") or
            chat_info.get("value", {}).get("city") or
            {}
        )
        if isinstance(location, dict):
            location_name = (
                location.get("name") or 
                location.get("city") or 
                location.get("title") or
                location.get("value") or
                ""
            )
        elif isinstance(location, str):
            location_name = location
        else:
            location_name = ""
        
        if location_name:
            chat_details.append(f"Локация: {location_name}")
    
    # Добавляем информацию о чате, даже если chat_info пустой
    if not chat_details:
        # Пробуем получить хотя бы chat_id
        if chat_id:
            chat_details.append(f"Chat ID: {chat_id}")
    
    # Формируем секцию ОТВЕТЫ (ответы менеджера из Telegram)
    # Пока оставляем пустым, так как нужно хранить историю ответов менеджера
    answers_section = "ОТВЕТЫ:\n\n"
    
    # Формируем финальное сообщение
    parts = [header]
    
    if history_text:
        parts.append("")
        parts.append("ИСТОРИЯ")
        parts.append("")
        parts.append(history_text)
    
    if chat_details:
        parts.append("")
        parts.extend(chat_details)
    
    parts.append("")
    parts.append(answers_section)
    parts.append(f"<code>{chat_id}</code>")
    
    return "\n".join(parts)


def extract_chat_id_from_webhook(data: Dict[str, Any]) -> Optional[str]:
    """
    Извлекает chat_id из webhook payload.
    
    Поддерживает разные форматы webhook от Avito (v3.0.0 и другие).
    
    Args:
        data: JSON данные от webhook
        
    Returns:
        chat_id или None если не найден
    """
    payload_value = (data.get("payload") or {}).get("value") or {}
    
    chat_id = (
        payload_value.get("chat_id")
        or data.get("chat_id")
        or (data.get("chat", {}) or {}).get("id")
    )
    
    return chat_id if chat_id else None


def extract_text_from_webhook(data: Dict[str, Any]) -> str:
    """
    Извлекает текст сообщения из webhook payload.
    
    Поддерживает разные форматы webhook от Avito.
    
    Args:
        data: JSON данные от webhook
        
    Returns:
        Текст сообщения или пустая строка
    """
    payload_value = (data.get("payload") or {}).get("value") or {}
    
    text = (
        (payload_value.get("content") or {}).get("text")
        or payload_value.get("text")
        or ((data.get("message") or {}).get("content") or {}).get("text")
        or (data.get("message") or {}).get("text")
        or data.get("text")
        or ""
    )
    
    return text


@app.route(HEALTH_ENDPOINT, methods=["GET"])
def health() -> tuple[str, int]:
    """
    Health check endpoint для мониторинга состояния сервиса.
    
    Returns:
        Кортеж ("ok", 200)
    """
    return "ok", 200


@app.route(WEBHOOK_ENDPOINT, methods=["POST"])
def avito_webhook() -> Response:
    """
    Обрабатывает webhook от Avito.
    
    Принимает сообщения от Avito, уведомляет менеджера в Telegram
    и генерирует автоматический ответ через LLM (если бот активен).
    
    Returns:
        JSON ответ с результатом обработки
    """
    data: Dict[str, Any] = request.json or {}
    
    # Логируем полный webhook payload для диагностики (только если AVITO_ACCOUNT_ID не установлен)
    if not AVITO_ACCOUNT_ID:
        logger.warning("⚠️ AVITO_ACCOUNT_ID not set! Logging full webhook payload to help find it:")
        logger.warning("Full webhook data: %s", data)
        logger.warning("Webhook JSON structure:\n%s", json.dumps(data, indent=2, ensure_ascii=False))
    
    # Извлекаем chat_id и текст
    chat_id = extract_chat_id_from_webhook(data)
    text = extract_text_from_webhook(data)

    if not chat_id:
        logger.warning("Webhook without chat_id: %s", data)
        return jsonify({"ok": False, "error": "no chat_id"}), 400

    logger.info("Received webhook: chat_id=%s, text_length=%d", chat_id, len(text) if text else 0)

    async def notify_and_maybe_reply() -> None:
        """
        Уведомляет менеджера и генерирует автоответ (если бот активен).
        
        Создаёт новый Bot instance для этого event loop, так как
        Flask работает в отдельном потоке без event loop.
        """
        # Создаём новый bot instance для этого event loop
        # Это необходимо, чтобы aiohttp timeout context manager работал корректно
        thread_bot = Bot(
            token=TELEGRAM_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode="HTML")
        )
        try:
            # Получаем данные из webhook payload для проверки направления
            webhook_payload_value = (data.get("payload") or {}).get("value") or {}
            webhook_data = data
            
            # Извлекаем метаданные сообщения
            direction = webhook_payload_value.get("direction") or webhook_data.get("direction")
            author_id = webhook_payload_value.get("author_id") or webhook_data.get("author_id")
            message_type = (
                webhook_payload_value.get("type") or
                webhook_payload_value.get("message_type") or
                webhook_data.get("type") or
                webhook_data.get("message_type") or
                ""
            )
            
            # Попытка определить AVITO_ACCOUNT_ID из webhook (если не установлен)
            if not AVITO_ACCOUNT_ID:
                potential_account_id = (
                    data.get("user_id") or
                    data.get("account_id") or
                    (data.get("payload") or {}).get("user_id") or
                    (data.get("payload") or {}).get("account_id")
                )
                if potential_account_id:
                    logger.warning(
                        "⚠️ AVITO_ACCOUNT_ID not set, but found potential account_id in webhook: %s",
                        potential_account_id
                    )
                    logger.warning("   Please set AVITO_ACCOUNT_ID=%s in your .env file", potential_account_id)
            
            # Проверяем, может ли токен получить доступ к этому чату
            if AVITO_ACCOUNT_ID:
                try:
                    from avito_api import get_chat
                    chat_info = get_chat(chat_id)
                    if chat_info:
                        logger.info("✓ Доступ к чату подтвержден, можно отправлять сообщения")
                    else:
                        logger.warning("⚠️ Не удалось получить информацию о чате - возможна проблема с правами")
                except Exception as e:
                    error_str = str(e).lower()
                    if "403" in error_str or "permission denied" in error_str:
                        logger.error("❌ 403 Permission Denied при проверке доступа к чату")
                        logger.error("   Это означает, что текущий account_id (%s) не имеет прав на этот чат",
                                    AVITO_ACCOUNT_ID)
                        logger.error("   Возможные решения:")
                        logger.error("   1. Убедитесь, что AVITO_ACCOUNT_ID = ID основного аккаунта компании (не сотрудника)")
                        logger.error("   2. Убедитесь, что AVITO_CLIENT_ID и AVITO_CLIENT_SECRET принадлежат компании")
                        logger.error("   3. Проверьте права приложения в личном кабинете Avito")
                    else:
                        logger.warning("Предупреждение при проверке чата: %s", e)
            
            logger.info(
                "Webhook message: chat_id=%s, direction=%s, author_id=%s, type=%s, text_length=%d",
                chat_id, direction, author_id, message_type, len(text) if text else 0
            )
            
            # ФИЛЬТРАЦИЯ 1: Игнорируем исходящие сообщения (от бота/компании)
            if direction == "out":
                logger.info("Ignoring outgoing message (from bot/company) for chat %s", chat_id)
                return
            
            # ФИЛЬТРАЦИЯ 2: Игнорируем сообщения, если они не входящие (должны быть "in")
            # Но если direction не указан, пропускаем (может быть другой формат webhook)
            if direction is not None and direction != "in":
                logger.info("Ignoring message with direction='%s' (expected 'in') for chat %s", direction, chat_id)
                return
            
            # ФИЛЬТРАЦИЯ 3: Игнорируем системные сообщения от Avito
            system_types = ["system", "service", "notification", "system_message"]
            if message_type and message_type.lower() in system_types:
                logger.info("Ignoring system message (type='%s') for chat %s", message_type, chat_id)
                return
            
            # ФИЛЬТРАЦИЯ 4: Проверяем, что сообщение не от нашего аккаунта (если author_id совпадает с account_id)
            if AVITO_ACCOUNT_ID and author_id:
                # Преобразуем в строки для сравнения
                author_id_str = str(author_id).strip()
                account_id_str = str(AVITO_ACCOUNT_ID).strip()
                if author_id_str == account_id_str:
                    logger.info(
                        "Ignoring message from our account (author_id=%s matches account_id=%s) for chat %s",
                        author_id_str, account_id_str, chat_id
                    )
                    return
            
            # ФИЛЬТРАЦИЯ 5: Если нет текста - не обрабатываем (может быть системное сообщение)
            if not text or not text.strip():
                logger.info("Empty text in webhook for chat %s, skipping (likely system message)", chat_id)
                return
            
            # ФИЛЬТРАЦИЯ 6: Игнорируем очень короткие сообщения (вероятно, системные)
            if len(text.strip()) < 2:
                logger.info("Ignoring very short message (length=%d) for chat %s", len(text.strip()), chat_id)
                return
            
            # ФИЛЬТРАЦИЯ 7: Проверяем системные префиксы в тексте
            system_prefixes = [
                "системное:",
                "system:",
                "уведомление:",
                "notification:",
                "сообщение отправлено",
                "message sent",
                "чат создан",
                "chat created",
            ]
            text_lower = text.strip().lower()
            if any(text_lower.startswith(prefix) for prefix in system_prefixes):
                logger.info("Ignoring message with system prefix for chat %s", chat_id)
                return
            
            # ФИЛЬТРАЦИЯ 8: Проверяем, что сообщение содержит реальный текст (не только специальные символы)
            # Удаляем пробелы и проверяем, остался ли текст
            text_without_spaces = text.strip().replace(" ", "").replace("\n", "").replace("\t", "")
            if len(text_without_spaces) < 2:
                logger.info("Ignoring message with only whitespace/special chars for chat %s", chat_id)
                return

            # Если бот должен молчать — выходим
            if not can_bot_reply(chat_id):
                logger.info("Bot is paused for chat %s (waiting_manager or cooldown)", chat_id)
                return

            # Проверяем конфигурацию перед отправкой
            if not AVITO_ACCOUNT_ID:
                logger.error("AVITO_ACCOUNT_ID not set! Cannot send message to Avito chat %s", chat_id)
                return

            # Генерируем автоответ ЕДИНЫМ модулем и отправляем в Avito
            logger.info("Generating auto-reply for chat %s, text_length=%d", chat_id, len(text))
            answer, meta = await generate_reply(dialog_id=f"avito_{chat_id}", incoming_text=text)
            logger.info("Generated reply for chat %s, answer_length=%d", chat_id, len(answer))
            
            logger.info(
                "Attempting to send message to Avito: account_id=%s, chat_id=%s",
                AVITO_ACCOUNT_ID, chat_id
            )
            ok = send_message(chat_id, answer)
            if ok:
                logger.info("✅ Auto-reply sent successfully to Avito chat %s", chat_id)
            else:
                logger.error(
                    "❌ Failed to send auto-reply to Avito chat %s - check logs above for details",
                    chat_id
                )
            
            # Если бот сообщил, что ответит менеджер — включаем бесконечную паузу
            if meta.get("contains_signal_phrase"):
                set_waiting_manager(chat_id)
            
            # Проверяем, содержит ли сообщение клиента или ответ бота сигнальные фразы
            text_lower = text.strip().lower()
            answer_lower = answer.lower()
            contains_signal = any(phrase.lower() in text_lower for phrase in SIGNAL_PHRASES) or \
                            any(phrase.lower() in answer_lower for phrase in SIGNAL_PHRASES)
            
            # Уведомляем менеджера ТОЛЬКО если есть сигнальная фраза
            if contains_signal or meta.get("contains_signal_phrase"):
                logger.info("Signal phrase detected in message or reply for chat %s", chat_id)
                
                # Получаем информацию о чате и историю сообщений из Avito
                chat_info: Optional[Dict[str, Any]] = None
                history: List[Dict[str, Any]] = []
                user_name: Optional[str] = None
                
                try:
                    # Получаем информацию о чате (объявление, аккаунт, собеседник, локация)
                    from avito_api import get_chat
                    chat_info = get_chat(chat_id)
                    if chat_info:
                        logger.info("Retrieved chat info for chat %s: %s", chat_id, json.dumps(chat_info, indent=2, ensure_ascii=False)[:500])
                        # Извлекаем имя пользователя из chat_info
                        user_data = chat_info.get("user") or chat_info.get("interlocutor") or chat_info.get("interlocutor_info") or {}
                        if isinstance(user_data, dict):
                            user_name = (
                                user_data.get("name") or
                                user_data.get("first_name") or
                                user_data.get("full_name") or
                                user_data.get("profile_name") or
                                user_data.get("username")
                            )
                    else:
                        logger.warning("get_chat returned None or empty for chat %s", chat_id)
                except Exception as e:
                    logger.warning("Failed to fetch chat info for chat %s: %s", chat_id, e)
                    logger.exception("Full exception details:")
                
                try:
                    logger.info("Fetching message history for chat %s", chat_id)
                    history = list_messages_v3(chat_id, limit=50, offset=0)
                    logger.info("Retrieved %d messages from history for chat %s", len(history), chat_id)
                    if history:
                        logger.debug("First message sample: %s", json.dumps(history[0] if history else {}, indent=2, ensure_ascii=False)[:300])
                except Exception as e:
                    logger.warning("Failed to fetch message history for chat %s: %s", chat_id, e)
                    logger.exception("Full exception details:")
                    # Продолжаем без истории, если не удалось получить
                
                # Извлекаем имя пользователя из webhook, если не получили из chat_info
                if not user_name:
                    webhook_payload_value = (data.get("payload") or {}).get("value") or {}
                    user_data = webhook_payload_value.get("user") or webhook_payload_value.get("interlocutor") or {}
                    if isinstance(user_data, dict):
                        user_name = (
                            user_data.get("name") or
                            user_data.get("first_name") or
                            user_data.get("full_name")
                        )
                
                # Формируем уведомление с историей
                notification_text = format_manager_text_with_history(
                    chat_id, text, history, chat_info=chat_info, user_name=user_name
                )
                
                # Отправляем уведомление менеджеру
                await safe_send_message_to_chat(
                    thread_bot,
                    TELEGRAM_MANAGER_ID,
                    notification_text
                )
                logger.info("Sent notification to manager for chat %s (contains signal phrase)", chat_id)
            else:
                logger.info("No signal phrase detected, skipping manager notification for chat %s", chat_id)
        finally:
            await thread_bot.session.close()

    run_async_in_thread(notify_and_maybe_reply())
    return jsonify({"ok": True})


# Менеджер отвечает в ТГ REPLY на уведомление (содержит Avito Chat ID)
@dp.message(F.reply_to_message & F.reply_to_message.from_user.id == bot.id)
async def manager_reply_handler(message: Message) -> None:
    """
    Обрабатывает reply менеджера на уведомление от бота.
    
    Извлекает Avito Chat ID из уведомления и отправляет ответ менеджера в Avito.
    
    Args:
        message: Сообщение от менеджера (reply на уведомление)
    """
    logger.info("Processing manager reply for Avito chat")
    
    replied = message.reply_to_message
    if not replied:
        logger.warning("manager_reply_handler: reply_to_message is None")
        return
    
    base_text = (replied.text or "") + "\n" + (replied.caption or "")

    # Извлекаем chat_id из уведомления - пробуем разные паттерны
    # Важно: chat_id может быть в формате u2i-...~... или u2u-...~...
    chat_id = None
    
    # Паттерн 1: Ищем <code>...</code> с полным chat_id (включая префикс u2i-/u2u- и тильду)
    code_match = re.search(r"<code>([uU]2[iIuU]-[0-9a-zA-Z_\-~]+)</code>", base_text)
    if code_match:
        chat_id = code_match.group(1).strip()
        logger.info("Extracted chat_id from <code> tag (pattern 1): %s", chat_id)
    
    # Паттерн 2: Ищем <code>...</code> с любым содержимым (fallback)
    if not chat_id:
        code_match = re.search(r"<code>([0-9a-zA-Z:_\-~]+)</code>", base_text)
        if code_match:
            potential_id = code_match.group(1).strip()
            # Проверяем, что это похоже на полный chat_id (должен содержать префикс или быть достаточно длинным)
            if potential_id.startswith(('u2i-', 'u2u-', 'U2I-', 'U2U-')) or len(potential_id) > 15:
                chat_id = potential_id
                logger.info("Extracted chat_id from <code> tag (pattern 2): %s", chat_id)
    
    # Паттерн 3: HTML формат "Avito Chat ID: <code>chat_id</code>"
    if not chat_id:
        m = CHAT_ID_PATTERN_HTML.search(base_text)
        if m:
            potential_id = (m.group(1) or m.group(2) or "").strip()
            if potential_id.startswith(('u2i-', 'u2u-', 'U2I-', 'U2U-')) or len(potential_id) > 15:
                chat_id = potential_id
                logger.info("Extracted chat_id from HTML pattern: %s", chat_id)
    
    # Паттерн 4: Текстовый формат "Avito Chat ID: chat_id"
    if not chat_id:
        m = CHAT_ID_PATTERN_TEXT.search(base_text)
        if m:
            potential_id = (m.group(1) or m.group(2) or "").strip()
            if potential_id.startswith(('u2i-', 'u2u-', 'U2I-', 'U2U-')) or len(potential_id) > 15:
                chat_id = potential_id
                logger.info("Extracted chat_id from text pattern: %s", chat_id)
    
    # Паттерн 5: Ищем строку в конце сообщения, которая начинается с u2i-/u2u- или длинная
    if not chat_id:
        lines = base_text.strip().split('\n')
        if lines:
            last_line = lines[-1].strip()
            # Chat ID обычно начинается с префикса типа "u2i-" или "u2u-" и содержит тильду
            if last_line.startswith(('u2i-', 'u2u-', 'U2I-', 'U2U-')):
                chat_id = last_line
                logger.info("Extracted chat_id from last line (has prefix): %s", chat_id)
            elif re.match(r'^[0-9a-zA-Z:_\-~]+$', last_line) and len(last_line) > 15:
                # Если строка длинная и похожа на chat_id, но без префикса - возможно это часть ID
                # Попробуем найти полный ID выше в тексте
                for line in reversed(lines[:-1]):
                    if re.match(r'^[uU]2[iIuU]-[0-9a-zA-Z_\-~]+$', line.strip()):
                        chat_id = line.strip()
                        logger.info("Extracted chat_id from previous line: %s", chat_id)
                        break
    
    # Логируем для отладки
    if chat_id:
        logger.info("Final extracted chat_id: %s (length: %d)", chat_id, len(chat_id))
        # Проверяем, что chat_id выглядит полным
        if not chat_id.startswith(('u2i-', 'u2u-', 'U2I-', 'U2U-')) and len(chat_id) < 20:
            logger.warning("Chat ID seems incomplete: %s (expected format: u2i-...~... or u2u-...~...)", chat_id)
    else:
        logger.warning("Could not extract chat_id from notification. Text preview: %s", base_text[:500])
        logger.warning("Full notification text: %s", base_text)

    if not chat_id:
        await safe_send_message(
            message, "Не удалось определить Avito Chat ID. Ответьте именно на уведомление бота с ID."
        )
        return

    text_to_send = message.text or ""
    if not text_to_send:
        await safe_send_message(message, "Пустое сообщение не отправлено.")
        return

    logger.info("Sending manager reply to Avito: chat_id=%s, text_length=%d", chat_id, len(text_to_send))
    ok = send_message(chat_id, text_to_send)
    if ok:
        logger.info("Manager reply sent successfully to chat_id=%s, setting cooldown", chat_id)
        set_cooldown_after_manager(chat_id)
        await safe_send_message(
            message, "Ответ менеджера отправлен в Avito. Бот снова активируется через 15 минут."
        )
    else:
        logger.error("Failed to send message to Avito chat_id=%s, text_length=%d", chat_id, len(text_to_send))
        # Не устанавливаем cooldown, если отправка не удалась
        await safe_send_message(
            message, f"Ошибка при отправке ответа в Avito (chat_id: {chat_id}). Проверьте логи/настройки."
        )


# Отправка без reply — если в тексте есть "Avito Chat ID: <id>"
@dp.message(F.text.regexp(r"(?i)Avito Chat ID[:\s]*([0-9a-zA-Z:_-]+)") & ~F.reply_to_message)
async def manager_send_by_text(message: Message) -> None:
    """
    Обрабатывает сообщение менеджера с Avito Chat ID в тексте.
    
    Позволяет отправить сообщение в Avito без reply, указав chat_id в тексте.
    Формат: "Avito Chat ID: <id> текст сообщения"
    
    Args:
        message: Сообщение от менеджера с chat_id в тексте
    """
    logger.info("Processing manager send by text with Avito Chat ID")
    
    txt = message.text or ""
    m = AVITO_CHAT_ID_PATTERN.search(txt)
    if not m:
        return  # Не должно быть, так как фильтр уже проверил, но на всякий случай
    
    chat_id = m.group(1).strip()
    text_to_send = AVITO_CHAT_ID_PATTERN.sub("", txt).strip()
    
    if not text_to_send:
        await safe_send_message(message, "После Avito Chat ID добавьте текст ответа для клиента.")
        return
    
    ok = send_message(chat_id, text_to_send)
    if ok:
        set_cooldown_after_manager(chat_id)
        await safe_send_message(
            message, "Ответ менеджера отправлен в Avito. Бот снова активируется через 15 минут."
        )
    else:
        logger.error("Failed to send message to Avito chat_id=%s, text_length=%d", chat_id, len(text_to_send))
        await safe_send_message(
            message, f"Ошибка при отправке ответа в Avito (chat_id: {chat_id}). Проверьте логи/настройки."
        )


def run_flask() -> None:
    """Запускает Flask сервер для обработки webhook."""
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)


async def run_bot() -> None:
    """Запускает Telegram бота через polling."""
    await dp.start_polling(bot)


if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем Telegram бота в основном потоке
    asyncio.run(run_bot())
