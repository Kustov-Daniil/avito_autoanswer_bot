"""
Инициализация Telegram бота и диспетчера.

Создает и настраивает экземпляры Bot и Dispatcher для работы с Telegram API.
"""
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from config import TELEGRAM_BOT_TOKEN

# Проверка наличия токена
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set in environment variables")

# Инициализация хранилища состояний
storage = MemoryStorage()

# Создание экземпляра бота с настройками по умолчанию
bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)

# Создание диспетчера
dp = Dispatcher(storage=storage)
