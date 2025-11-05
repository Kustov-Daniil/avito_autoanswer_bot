"""
Конфигурация приложения.

Загружает переменные окружения из .env файла и предоставляет
централизованный доступ к настройкам приложения.
"""

import os
from typing import Optional, List
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()


def _parse_admins(admins_str: Optional[str]) -> List[int]:
    """
    Парсит строку с ID администраторов.
    
    Args:
        admins_str: Строка с ID администраторов, разделёнными запятыми
        
    Returns:
        Список целых чисел (ID администраторов)
    """
    if not admins_str:
        return []
    try:
        return [int(admin_id.strip()) for admin_id in admins_str.split(",") if admin_id.strip()]
    except ValueError:
        return []


# Telegram
TELEGRAM_BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_MANAGER_ID: int = int(os.getenv("TELEGRAM_MANAGER_ID", "0"))

# Avito
AVITO_CLIENT_ID: Optional[str] = os.getenv("AVITO_CLIENT_ID")
AVITO_CLIENT_SECRET: Optional[str] = os.getenv("AVITO_CLIENT_SECRET")
AVITO_ACCOUNT_ID: Optional[str] = os.getenv("AVITO_ACCOUNT_ID")  # user_id компании (основной аккаунт)

# OpenAI / LLM
OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o")
TEMPERATURE: float = float(os.getenv("TEMPERATURE", "0.2"))

# Менеджерская логика
COOLDOWN_MINUTES_AFTER_MANAGER: int = int(os.getenv("COOLDOWN_MINUTES_AFTER_MANAGER", "15"))

# Сигнальные фразы для передачи диалога менеджеру
SIGNAL_PHRASES: List[str] = [
    "по данному вопросу вам в ближайшее время ответит наш менеджер",
    "ответит наш менеджер",
    "наш менеджер ответит",
    "свяжется менеджер",
    "свяжется наш менеджер",
]

# Пути к файлам данных
DATA_DIR: str = "data"
FAQ_PATH: str = os.path.join(DATA_DIR, "faq.json")
STATIC_CONTEXT_PATH: str = os.path.join(DATA_DIR, "static_context.txt")
CHAT_HISTORY_PATH: str = os.path.join(DATA_DIR, "chat_history.json")

# Публичная база (для вебхука)
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_URL: str = f"{PUBLIC_BASE_URL}/avito/webhook" if PUBLIC_BASE_URL else ""

# Администраторы (парсим из строки)
ADMINS: List[int] = _parse_admins(os.getenv("ADMINS"))
