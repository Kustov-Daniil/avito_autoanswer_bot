"""
Конфигурация приложения.

Загружает переменные окружения из .env файла и предоставляет
централизованный доступ к настройкам приложения.
"""

import os
import logging
import json
from typing import Optional, List, Tuple, Dict, Any
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

def _parse_bool(value: Optional[str], *, default: bool = False) -> bool:
    """
    Парсит булево значение из env.

    True считаем для: 1/true/yes/y/on (в любом регистре).
    """
    if value is None:
        return default
    v = str(value).strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "y", "on"}


def _parse_admins(admins_str: Optional[str]) -> List[int]:
    """
    Парсит строку с ID администраторов.
    
    Поддерживает форматы:
    - "123,456,789" (строка через запятую)
    - "[123, 456, 789]" (список Python)
    - "[123,456,789]" (список Python без пробелов)
    
    Args:
        admins_str: Строка с ID администраторов
        
    Returns:
        Список целых чисел (ID администраторов)
    """
    if not admins_str:
        return []
    
    # Убираем пробелы
    admins_str = admins_str.strip()
    
    # Если это список Python (начинается с [ и заканчивается ])
    if admins_str.startswith("[") and admins_str.endswith("]"):
        # Убираем скобки и парсим
        admins_str = admins_str[1:-1].strip()
    
    try:
        # Парсим через запятую
        admin_ids = [int(admin_id.strip()) for admin_id in admins_str.split(",") if admin_id.strip()]
        return admin_ids
    except ValueError:
        return []


# Telegram
TELEGRAM_BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_MANAGER_ID: int = int(os.getenv("TELEGRAM_MANAGER_ID", "0"))  # Для обратной совместимости

# Менеджеры (парсим из строки MANAGERS, если не указано - используем TELEGRAM_MANAGER_ID)
def _parse_managers(managers_str: Optional[str], fallback_id: int) -> List[int]:
    """
    Парсит строку с ID менеджеров.
    
    Поддерживает форматы:
    - "123,456,789" (строка через запятую)
    - "[123, 456, 789]" (список Python)
    - "[123,456,789]" (список Python без пробелов)
    
    Args:
        managers_str: Строка с ID менеджеров
        fallback_id: ID менеджера для обратной совместимости
        
    Returns:
        Список целых чисел (ID менеджеров)
    """
    if managers_str:
        # Убираем пробелы
        managers_str = managers_str.strip()
        
        # Если это список Python (начинается с [ и заканчивается ])
        if managers_str.startswith("[") and managers_str.endswith("]"):
            # Убираем скобки и парсим
            managers_str = managers_str[1:-1].strip()
        
        try:
            # Парсим через запятую
            managers = [int(manager_id.strip()) for manager_id in managers_str.split(",") if manager_id.strip()]
            if managers:
                return managers
        except ValueError:
            pass
    # Если MANAGERS не указан или пуст, используем fallback
    if fallback_id and fallback_id > 0:
        return [fallback_id]
    return []

# Парсим менеджеров из MANAGERS
_parsed_managers: List[int] = _parse_managers(os.getenv("MANAGERS"), TELEGRAM_MANAGER_ID)

# Администраторы (парсим из строки)
ADMINS: List[int] = _parse_admins(os.getenv("ADMINS"))

# Менеджеры = MANAGERS + ADMINS (ADMINS автоматически получают уведомления)
# Объединяем списки, убирая дубликаты
TELEGRAM_MANAGERS: List[int] = list(set(_parsed_managers + ADMINS))

# Avito
AVITO_CLIENT_ID: Optional[str] = os.getenv("AVITO_CLIENT_ID")
AVITO_CLIENT_SECRET: Optional[str] = os.getenv("AVITO_CLIENT_SECRET")
AVITO_ACCOUNT_ID: Optional[str] = os.getenv("AVITO_ACCOUNT_ID")  # user_id компании (основной аккаунт)

# Локальное хранилище credentials, которое может обновляться ботом из Telegram.
# ВАЖНО: этот файл должен быть в .gitignore (секреты не должны попадать в git).
AVITO_CREDENTIALS_STORE_PATH: str = os.getenv(
    "AVITO_CREDENTIALS_STORE_PATH",
    os.path.join("data", "avito_credentials.json"),
).strip()

# Multi-account credentials (безопасно): secrets храним только в .env, не в JSON-файлах.
#
# Вариант 1 (рекомендуется): JSON в env переменной (удобно для нескольких аккаунтов):
#   AVITO_ACCOUNTS_CREDENTIALS_JSON='{"417713955": {"client_id": "...", "client_secret": "..."}}'
#
# Вариант 2 (fallback): глобальные AVITO_CLIENT_ID/AVITO_CLIENT_SECRET (одни на все аккаунты).
AVITO_ACCOUNTS_CREDENTIALS_JSON: str = os.getenv("AVITO_ACCOUNTS_CREDENTIALS_JSON", "").strip()


def _parse_avito_accounts_credentials(raw: str) -> Dict[str, Dict[str, str]]:
    """
    Парсит карту credentials из env.

    Ожидаемый формат:
      {"<account_id>": {"client_id": "...", "client_secret": "..."}, ...}
    """
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        logging.warning("AVITO_ACCOUNTS_CREDENTIALS_JSON is not valid JSON - ignoring")
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for k, v in obj.items():
        aid = str(k).strip()
        if not aid or not aid.isdigit() or not isinstance(v, dict):
            continue
        cid = str(v.get("client_id") or "").strip()
        csec = str(v.get("client_secret") or "").strip()
        if cid and csec:
            out[aid] = {"client_id": cid, "client_secret": csec}
    return out


_AVITO_ACCOUNTS_CREDS: Dict[str, Dict[str, str]] = _parse_avito_accounts_credentials(AVITO_ACCOUNTS_CREDENTIALS_JSON)


def _load_avito_credentials_store(path: str) -> Dict[str, Dict[str, str]]:
    """
    Загружает локальное хранилище credentials (обновляемое через Telegram, без рестарта).

    Формат файла:
      {
        "<account_id>": {"client_id": "...", "client_secret": "...", "updated_at": "..."},
        ...
      }
    """
    if not path:
        return {}
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for k, v in obj.items():
        aid = str(k).strip()
        if not aid or not aid.isdigit() or not isinstance(v, dict):
            continue
        cid = str(v.get("client_id") or "").strip()
        csec = str(v.get("client_secret") or "").strip()
        if cid and csec:
            out[aid] = {"client_id": cid, "client_secret": csec}
    return out


def get_avito_credentials(account_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает (client_id, client_secret) для account_id.

    Приоритет:
    0) AVITO_CREDENTIALS_STORE_PATH (локальный файл, обновляемый через Telegram)
    1) AVITO_ACCOUNTS_CREDENTIALS_JSON (per-account)
    2) AVITO_CLIENT_ID / AVITO_CLIENT_SECRET (глобальные)
    """
    aid = str(account_id or "").strip()
    if aid and aid.isdigit():
        store = _load_avito_credentials_store(AVITO_CREDENTIALS_STORE_PATH)
        item0 = store.get(aid)
        if item0:
            return item0.get("client_id"), item0.get("client_secret")
    if aid and aid.isdigit():
        item = _AVITO_ACCOUNTS_CREDS.get(aid)
        if item:
            return item.get("client_id"), item.get("client_secret")
    # fallback на глобальные
    cid = str(AVITO_CLIENT_ID).strip() if AVITO_CLIENT_ID else None
    csec = str(AVITO_CLIENT_SECRET).strip() if AVITO_CLIENT_SECRET else None
    return cid, csec


def has_avito_credentials(account_id: Optional[str]) -> bool:
    cid, csec = get_avito_credentials(account_id)
    return bool(cid and csec)

# OpenAI / LLM
OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
_default_llm_model: str = os.getenv("LLM_MODEL", "gpt-4o")
# Загружаем модель из сохраненного состояния, если есть, иначе из .env
# Используем ленивую загрузку через функцию, чтобы избежать циклических импортов
def _get_llm_model() -> str:
    """Получает модель LLM из сохраненного состояния или из .env."""
    try:
        from avito_sessions import get_llm_model
        return get_llm_model(_default_llm_model)
    except Exception:
        return _default_llm_model

LLM_MODEL: str = _get_llm_model()
TEMPERATURE: float = float(os.getenv("TEMPERATURE", "0.2"))

# Менеджерская логика
COOLDOWN_MINUTES_AFTER_MANAGER: int = int(os.getenv("COOLDOWN_MINUTES_AFTER_MANAGER", "15"))

# FAQ из истории
MIN_DIALOG_AGE_MINUTES: int = int(os.getenv("MIN_DIALOG_AGE_MINUTES", "5"))  # Минимальный возраст диалога в минутах для обработки FAQ

# Экономические метрики
MANAGER_COST_PER_HOUR: float = float(os.getenv("MANAGER_COST_PER_HOUR", "1000"))  # Стоимость менеджера в рублях за час
USD_RATE: float = float(os.getenv("USD_RATE", "100"))  # Курс доллара к рублю

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
KNOWLEDGE_CARDS_PATH: str = os.path.join(DATA_DIR, "knowledge_cards.json")
AVITO_ACCOUNTS_PATH: str = os.path.join(DATA_DIR, "avito_accounts.json")
STATIC_CONTEXT_PATH: str = os.path.join(DATA_DIR, "static_context.txt")
DYNAMIC_CONTEXT_PATH: str = os.path.join(DATA_DIR, "dynamic_context.txt")
SYSTEM_PROMPT_PATH: str = os.path.join(DATA_DIR, "system_prompt.txt")
BOT_STATE_PATH: str = os.path.join(DATA_DIR, "bot_state.json")
CHAT_HISTORY_PATH: str = os.path.join(DATA_DIR, "chat_history.json")
VERSION_PATH: str = "version.txt"  # Файл версии (не в data/, чтобы коммитился)

# Публичная база (для вебхука)
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_URL: str = f"{PUBLIC_BASE_URL}/avito/webhook" if PUBLIC_BASE_URL else ""

# -----------------------------
# Webhook / Server (Flask)
# -----------------------------
# Где слушает Flask внутри сервера (обычно 0.0.0.0)
FLASK_HOST: str = os.getenv("FLASK_HOST", "0.0.0.0").strip() or "0.0.0.0"

# Порт Flask (по умолчанию 8080)
FLASK_PORT: int = int(os.getenv("FLASK_PORT", "8080"))

# URL-пути эндпоинтов
AVITO_WEBHOOK_ENDPOINT: str = os.getenv("AVITO_WEBHOOK_ENDPOINT", "/avito/webhook").strip() or "/avito/webhook"
HEALTH_ENDPOINT: str = os.getenv("HEALTH_ENDPOINT", "/health").strip() or "/health"

# -----------------------------
# Limits
# -----------------------------
# Ограничение Avito на длину сообщения (лучше держать небольшой запас)
MAX_AVITO_MESSAGE_LENGTH: int = int(os.getenv("MAX_AVITO_MESSAGE_LENGTH", "950"))

# Logging
# LOG_LEVEL: DEBUG/INFO/WARNING/ERROR (по умолчанию INFO)
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

# Логировать ли входящие webhook payload целиком (может содержать PII) — по умолчанию НЕТ
LOG_WEBHOOK_PAYLOAD: bool = _parse_bool(os.getenv("LOG_WEBHOOK_PAYLOAD"), default=False)

# Разрешить ли логирование “пользовательского контента” (PII/история/тексты) — по умолчанию НЕТ
LOG_PII: bool = _parse_bool(os.getenv("LOG_PII"), default=False)


def get_bot_version() -> str:
    """
    Получает версию бота из файла version.txt.
    
    Returns:
        Версия бота или "unknown" если файл не найден
    """
    try:
        if os.path.exists(VERSION_PATH):
            with open(VERSION_PATH, "r", encoding="utf-8") as f:
                version = f.read().strip()
                return version if version else "unknown"
        else:
            # Если файла нет, создаем с дефолтной версией
            default_version = "1.0.0"
            os.makedirs(os.path.dirname(VERSION_PATH) if os.path.dirname(VERSION_PATH) else ".", exist_ok=True)
            with open(VERSION_PATH, "w", encoding="utf-8") as f:
                f.write(default_version)
            return default_version
    except Exception as e:
        logging.warning("Failed to read bot version: %s", e)
        return "unknown"
