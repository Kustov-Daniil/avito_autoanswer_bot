
import os
from dotenv import load_dotenv

load_dotenv()
ADMINS = os.getenv("ADMINS")


# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_MANAGER_ID = int(os.getenv("TELEGRAM_MANAGER_ID", "0"))

# Avito
AVITO_CLIENT_ID = os.getenv("AVITO_CLIENT_ID")
AVITO_CLIENT_SECRET = os.getenv("AVITO_CLIENT_SECRET")
AVITO_ACCOUNT_ID = os.getenv("AVITO_ACCOUNT_ID")  # <- это user_id компании (основной аккаунт)

# OpenAI / LLM
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.2))

# Менеджерская логика
COOLDOWN_MINUTES_AFTER_MANAGER = int(os.getenv("COOLDOWN_MINUTES_AFTER_MANAGER", "15"))

SIGNAL_PHRASES = [
    "по данному вопросу вам в ближайшее время ответит наш менеджер",
    "ответит наш менеджер",
    "наш менеджер ответит",
    "свяжется менеджер",
    "свяжется наш менеджер",
]

# Пути
DATA_DIR = "data"
FAQ_PATH = os.path.join(DATA_DIR, "faq.json")
STATIC_CONTEXT_PATH = os.path.join(DATA_DIR, "static_context.txt")
CHAT_HISTORY_PATH = os.path.join(DATA_DIR, "chat_history.json")

# Публичная база (для вебхука)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_URL = f"{PUBLIC_BASE_URL}/avito/webhook" if PUBLIC_BASE_URL else ""

