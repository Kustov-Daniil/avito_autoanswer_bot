"""
Инициализация Telegram бота и диспетчера.

Создает и настраивает экземпляры Bot и Dispatcher для работы с Telegram API.
"""
from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from config import TELEGRAM_BOT_TOKEN

# Инициализация хранилища состояний
storage = MemoryStorage()

# Создание экземпляра бота с настройками по умолчанию.
# ВАЖНО: не падаем на import-time, чтобы можно было запускать утилиты/проверки без секретов.
bot: Bot | None = None
if TELEGRAM_BOT_TOKEN:
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )

# Создание диспетчера
dp = Dispatcher(storage=storage)
