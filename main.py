import asyncio
import threading
import logging
import re
import json
from flask import Flask, request, jsonify
from aiogram import F
from aiogram.types import Message

from create_bot import bot, dp
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from config import (
    TELEGRAM_MANAGER_ID, TELEGRAM_BOT_TOKEN,
    AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_ACCOUNT_ID
)
from avito_api import send_message
from avito_sessions import can_bot_reply, set_waiting_manager, set_cooldown_after_manager
from responder import generate_reply
from user_bot import user_router  # подключаем Router с админ-командами и ТГ-диалогом
from telegram_utils import safe_send_message, safe_send_message_to_chat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Проверяем конфигурацию при старте
def check_config():
    """Проверяет наличие всех необходимых переменных окружения."""
    missing = []
    
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

# Регистрируем твой router (команды/ответы в TG)
dp.include_router(user_router)


def run_async_in_thread(coro):
    """Запускает async функцию в отдельном потоке с собственным event loop."""
    def run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Создаём wrapper который создаёт задачу внутри запущенного loop
            async def wrapper():
                # Создаём задачу внутри запущенного loop - это важно для aiohttp
                task = asyncio.create_task(coro)
                # Возвращаем результат задачи
                return await task
            
            # run_until_complete создаст wrapper как задачу, и внутри wrapper будет создана задача для coro
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
            except Exception:
                pass
            loop.close()
    
    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()

def format_manager_text(chat_id: str, avito_text: str) -> str:
    return (
        "Новое сообщение с Avito:\n\n"
        f"{avito_text}\n\n"
        f"Avito Chat ID: <code>{chat_id}</code>\n\n"
        "Ответьте на это сообщение (reply) в Telegram, чтобы отправить ответ в Avito."
    )

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app.route("/avito/webhook", methods=["POST"])
def avito_webhook():
    data = request.json or {}
    
    # Логируем полный webhook payload для диагностики (только если AVITO_ACCOUNT_ID не установлен)
    if not AVITO_ACCOUNT_ID:
        logger.warning("⚠️ AVITO_ACCOUNT_ID not set! Logging full webhook payload to help find it:")
        logger.warning("Full webhook data: %s", data)
        # Попробуем найти account_id в структуре
        logger.warning("Webhook JSON structure:\n%s", json.dumps(data, indent=2, ensure_ascii=False))

    # Извлекаем chat_id и текст (подстрахуем разные формы)
    # Поддержка v3.0.0 формата: payload.value.chat_id и payload.value.content.text
    payload_value = (data.get("payload") or {}).get("value") or {}
    
    chat_id = (
        payload_value.get("chat_id")
        or data.get("chat_id")
        or (data.get("chat", {}) or {}).get("id")
    )
    
    text = (
        (payload_value.get("content") or {}).get("text")
        or payload_value.get("text")
        or ((data.get("message") or {}).get("content") or {}).get("text")
        or (data.get("message") or {}).get("text")
        or data.get("text")
        or ""
    )

    if not chat_id:
        logger.warning("Webhook without chat_id: %s", data)
        return jsonify({"ok": False, "error": "no chat_id"}), 400

    async def notify_and_maybe_reply():
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
            
            # Проверяем направление сообщения - игнорируем исходящие сообщения от бота
            direction = webhook_payload_value.get("direction") or webhook_data.get("direction")
            author_id = webhook_payload_value.get("author_id") or webhook_data.get("author_id")
            
            # Попытка определить AVITO_ACCOUNT_ID из webhook (если не установлен)
            # Обычно account_id можно найти в структуре webhook или из author_id
            if not AVITO_ACCOUNT_ID:
                # Попробуем извлечь из структуры webhook
                potential_account_id = (
                    data.get("user_id") or
                    data.get("account_id") or
                    (data.get("payload") or {}).get("user_id") or
                    (data.get("payload") or {}).get("account_id")
                )
                if potential_account_id:
                    logger.warning("⚠️ AVITO_ACCOUNT_ID not set, but found potential account_id in webhook: %s", potential_account_id)
                    logger.warning("   Please set AVITO_ACCOUNT_ID=%s in your .env file", potential_account_id)
            
            # Проверяем, может ли токен получить доступ к этому чату
            # Это поможет выявить проблему с правами доступа до попытки отправки
            if AVITO_ACCOUNT_ID:
                try:
                    from avito_api import get_chat
                    chat_info = get_chat(chat_id)
                    if chat_info:
                        logger.info("✓ Доступ к чату подтвержден, можно отправлять сообщения")
                    else:
                        logger.warning("⚠️ Не удалось получить информацию о чате - возможна проблема с правами")
                except Exception as e:
                    if "403" in str(e) or "permission denied" in str(e).lower():
                        logger.error("❌ 403 Permission Denied при проверке доступа к чату")
                        logger.error("   Это означает, что текущий account_id (%s) не имеет прав на этот чат", AVITO_ACCOUNT_ID)
                        logger.error("   Возможные решения:")
                        logger.error("   1. Убедитесь, что AVITO_ACCOUNT_ID = ID основного аккаунта компании (не сотрудника)")
                        logger.error("   2. Убедитесь, что AVITO_CLIENT_ID и AVITO_CLIENT_SECRET принадлежат компании")
                        logger.error("   3. Проверьте права приложения в личном кабинете Avito")
                        # Не прерываем выполнение, но предупреждаем
                    else:
                        logger.warning("Предупреждение при проверке чата: %s", e)
            
            logger.info("Webhook message: chat_id=%s, direction=%s, author_id=%s, text_length=%d", 
                       chat_id, direction, author_id, len(text) if text else 0)
            
            # Игнорируем исходящие сообщения (от бота/компании)
            if direction == "out":
                logger.info("Ignoring outgoing message (from bot) for chat %s", chat_id)
                return
            
            # Если нет текста - не обрабатываем
            if not text or not text.strip():
                logger.info("Empty text in webhook for chat %s, skipping", chat_id)
                return
            
            # Уведомим менеджера
            await safe_send_message_to_chat(
                thread_bot, TELEGRAM_MANAGER_ID, format_manager_text(chat_id, text)
            )

            # Если бот должен молчать — выходим
            if not can_bot_reply(chat_id):
                logger.info("Bot is paused for chat %s (waiting_manager or cooldown)", chat_id)
                return

            # Проверяем конфигурацию перед отправкой
            if not AVITO_ACCOUNT_ID:
                logger.error("AVITO_ACCOUNT_ID not set! Cannot send message to Avito chat %s", chat_id)
                await safe_send_message_to_chat(
                    thread_bot,
                    TELEGRAM_MANAGER_ID,
                    f"⚠️ Ошибка: AVITO_ACCOUNT_ID не установлен в .env файле. "
                    f"Сообщение не может быть отправлено в Avito (chat_id: {chat_id})"
                )
                return
            
            # Генерим автоответ ЕДИНЫМ модулем и отправляем в Avito
            logger.info("Generating auto-reply for chat %s, text_length=%d", chat_id, len(text))
            answer, meta = await generate_reply(dialog_id=f"avito_{chat_id}", incoming_text=text)
            logger.info("Generated reply for chat %s, answer_length=%d", chat_id, len(answer))
            
            logger.info("Attempting to send message to Avito: account_id=%s, chat_id=%s", 
                       AVITO_ACCOUNT_ID, chat_id)
            ok = send_message(chat_id, answer)
            if ok:
                logger.info("✅ Auto-reply sent successfully to Avito chat %s", chat_id)
            else:
                logger.error("❌ Failed to send auto-reply to Avito chat %s - check logs above for details", chat_id)

            # Если бот сообщил, что ответит менеджер — включаем бесконечную паузу и уведомляем
            if meta.get("contains_signal_phrase"):
                set_waiting_manager(chat_id)
                await safe_send_message_to_chat(
                    thread_bot,
                    TELEGRAM_MANAGER_ID,
                    f"Бот передал диалог менеджеру. Avito Chat ID: <code>{chat_id}</code>\n"
                    f"Сообщение клиента: {text}"
                )
        finally:
            await thread_bot.session.close()

    run_async_in_thread(notify_and_maybe_reply())
    return jsonify({"ok": True})

# Менеджер отвечает в ТГ REPLY на уведомление (содержит Avito Chat ID)
# ВАЖНО: Используем более специфичный фильтр, чтобы не перехватывать все reply
@dp.message(F.reply_to_message & F.reply_to_message.from_user.id == bot.id)
async def manager_reply_handler(message: Message):
    logger.info("Processing manager reply for Avito chat")
    
    replied = message.reply_to_message
    base_text = (replied.text or "") + "\n" + (replied.caption or "")

    m = (
        re.search(r"Avito Chat ID:\s*<code>(.*?)</code>", base_text)
        or re.search(r"Avito Chat ID:\s*([0-9a-zA-Z:_-]+)", base_text)
    )
    chat_id = m.group(1).strip() if m else None

    if not chat_id:
        await safe_send_message(
            message, "Не удалось определить Avito Chat ID. Ответьте именно на уведомление бота с ID."
        )
        return

    text_to_send = message.text or ""
    if not text_to_send:
        await safe_send_message(message, "Пустое сообщение не отправлено.")
        return

    ok = send_message(chat_id, text_to_send)
    if ok:
        set_cooldown_after_manager(chat_id)
        await safe_send_message(
            message, "Ответ менеджера отправлен в Avito. Бот снова активируется через 15 минут."
        )
    else:
        logger.error(f"Failed to send message to Avito chat_id={chat_id}, text_length={len(text_to_send)}")
        await safe_send_message(
            message, f"Ошибка при отправке ответа в Avito (chat_id: {chat_id}). Проверьте логи/настройки."
        )

# Отправка без reply — если в тексте есть "Avito Chat ID: <id>"
# ВАЖНО: Используем фильтр, чтобы обрабатывать только сообщения с "Avito Chat ID"
@dp.message(F.text.regexp(r"(?i)Avito Chat ID[:\s]*([0-9a-zA-Z:_-]+)") & ~F.reply_to_message)
async def manager_send_by_text(message: Message):
    logger.info("Processing manager send by text with Avito Chat ID")
    txt = message.text or ""
    m = re.search(r"Avito Chat ID[:\s]*([0-9a-zA-Z:_-]+)", txt, re.IGNORECASE)
    if not m:
        return  # Не должно быть, так как фильтр уже проверил, но на всякий случай
    chat_id = m.group(1).strip()
    text_to_send = re.sub(r"Avito Chat ID[:\s]*([0-9a-zA-Z:_-]+)", "", txt, flags=re.IGNORECASE).strip()
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
        logger.error(f"Failed to send message to Avito chat_id={chat_id}, text_length={len(text_to_send)}")
        await safe_send_message(
            message, f"Ошибка при отправке ответа в Avito (chat_id: {chat_id}). Проверьте логи/настройки."
        )

def run_flask():
    app.run(host="0.0.0.0", port=8080)

async def run_bot():
    await dp.start_polling(bot)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    asyncio.run(run_bot())
