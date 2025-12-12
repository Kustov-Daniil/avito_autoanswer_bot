"""
Управление сессиями чатов для контроля активности бота.

Модуль управляет состояниями чатов (waiting_manager, cooldown) для контроля,
когда бот должен отвечать автоматически, а когда ждать ответа менеджера.
Также управляет глобальным состоянием бота (ON/OFF).
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import os
import json
from config import COOLDOWN_MINUTES_AFTER_MANAGER, BOT_STATE_PATH
import logging

logger = logging.getLogger(__name__)

# Типы состояний
SESSION_STATE_WAITING_MANAGER: str = "waiting_manager"
SESSION_STATE_COOLDOWN: str = "cooldown"

# In-memory storage (для production можно заменить на Redis/БД)
_sessions: Dict[str, Dict[str, Any]] = {}  # chat_id -> {"state": str, "until": Optional[datetime]}

# Режимы работы бота
BOT_MODE_LISTENING: str = "listening"  # Только слушает, формирует FAQ
BOT_MODE_PARTIAL: str = "partial"      # Отвечает на часть сообщений (процент)
BOT_MODE_FULL: str = "full"            # Отвечает всем (полный режим)

# Глобальное состояние бота
_bot_enabled: bool = True
_bot_mode: str = BOT_MODE_FULL  # Режим работы бота
_partial_percentage: int = 50   # Процент сообщений для режима partial (0-100)


def _load_bot_state() -> bool:
    """Загружает состояние бота из файла."""
    global _bot_enabled, _bot_mode, _partial_percentage
    try:
        if os.path.exists(BOT_STATE_PATH):
            with open(BOT_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                _bot_enabled = data.get("enabled", True)
                _bot_mode = data.get("mode", BOT_MODE_FULL)
                _partial_percentage = data.get("partial_percentage", 50)
                logger.info(
                    "Загружено состояние бота из файла: enabled=%s, mode=%s, partial_percentage=%d",
                    "ON" if _bot_enabled else "OFF", _bot_mode, _partial_percentage
                )
        else:
            _bot_enabled = True
            _bot_mode = BOT_MODE_FULL
            _partial_percentage = 50
            _save_bot_state()
    except Exception as e:
        logger.warning("Не удалось загрузить состояние бота: %s, используем значения по умолчанию", e)
        _bot_enabled = True
        _bot_mode = BOT_MODE_FULL
        _partial_percentage = 50
    return _bot_enabled


def get_llm_model(default_model: str = "gpt-4o") -> str:
    """
    Получает сохраненную модель LLM из файла.
    
    Args:
        default_model: Модель по умолчанию, если не сохранена
    
    Returns:
        Название модели LLM
    """
    try:
        if os.path.exists(BOT_STATE_PATH):
            with open(BOT_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                saved_model = data.get("llm_model")
                if saved_model:
                    logger.info("Загружена модель LLM из файла: %s", saved_model)
                    return saved_model
    except Exception as e:
        logger.warning("Не удалось загрузить модель LLM: %s, используем значение по умолчанию", e)
    return default_model


def set_llm_model(model: str) -> None:
    """
    Сохраняет выбранную модель LLM в файл.
    
    Args:
        model: Название модели LLM (например, "gpt-5", "gpt-5-mini", "gpt-4o")
    """
    try:
        # Загружаем текущее состояние
        data = {}
        if os.path.exists(BOT_STATE_PATH):
            with open(BOT_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        
        # Обновляем модель
        data["llm_model"] = model
        
        # Сохраняем обратно
        os.makedirs(os.path.dirname(BOT_STATE_PATH), exist_ok=True)
        with open(BOT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info("Модель LLM сохранена: %s", model)
    except Exception as e:
        logger.error("Не удалось сохранить модель LLM: %s", e)


def _save_bot_state() -> None:
    """Сохраняет состояние бота в файл."""
    try:
        # Загружаем текущее состояние, чтобы сохранить модель LLM
        data = {}
        if os.path.exists(BOT_STATE_PATH):
            with open(BOT_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        
        # Обновляем состояние бота
        data["enabled"] = _bot_enabled
        data["mode"] = _bot_mode
        data["partial_percentage"] = _partial_percentage
        
        os.makedirs(os.path.dirname(BOT_STATE_PATH), exist_ok=True)
        with open(BOT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Не удалось сохранить состояние бота: %s", e)


def set_bot_enabled(enabled: bool) -> None:
    """
    Устанавливает глобальное состояние бота (ON/OFF).
    
    Args:
        enabled: True для включения бота, False для выключения
    """
    global _bot_enabled
    _bot_enabled = enabled
    _save_bot_state()
    logger.info("Состояние бота изменено: %s", "ON" if enabled else "OFF")


def is_bot_enabled() -> bool:
    """
    Проверяет, включен ли бот глобально.
    
    Returns:
        True если бот включен, False если выключен
    """
    return _bot_enabled


# Загружаем состояние бота при импорте
_load_bot_state()


def set_waiting_manager(chat_id: str) -> None:
    """
    Включает бесконечную паузу до первого ответа менеджера.
    
    Args:
        chat_id: ID чата в Avito
    """
    if not chat_id:
        logger.warning("set_waiting_manager called with empty chat_id")
        return
    
    _sessions[chat_id] = {"state": SESSION_STATE_WAITING_MANAGER, "until": None}
    logger.info("Set waiting_manager state for chat_id=%s", chat_id)


def set_cooldown_after_manager(chat_id: str, minutes: Optional[int] = None) -> None:
    """
    Устанавливает паузу после ответа менеджера.
    
    После ответа менеджера бот будет молчать указанное время (по умолчанию 15 минут),
    затем снова станет активным.
    
    Args:
        chat_id: ID чата в Avito
        minutes: Количество минут паузы (по умолчанию из конфига)
    """
    if not chat_id:
        logger.warning("set_cooldown_after_manager called with empty chat_id")
        return
    
    mins = minutes if minutes is not None else COOLDOWN_MINUTES_AFTER_MANAGER
    if mins < 0:
        logger.warning("Invalid cooldown minutes: %d, using default", mins)
        mins = COOLDOWN_MINUTES_AFTER_MANAGER
    
    _sessions[chat_id] = {
        "state": SESSION_STATE_COOLDOWN,
        "until": datetime.now() + timedelta(minutes=mins),
    }
    logger.info("Set cooldown for chat_id=%s, minutes=%d", chat_id, mins)


def clear_session(chat_id: str) -> None:
    """
    Очищает сессию для указанного чата.
    
    Args:
        chat_id: ID чата в Avito
    """
    if chat_id in _sessions:
        _sessions.pop(chat_id)
        logger.debug("Cleared session for chat_id=%s", chat_id)


def can_bot_reply(chat_id: str) -> bool:
    """
    Проверяет, может ли бот отвечать в указанном чате.
    
    Args:
        chat_id: ID чата в Avito
        
    Returns:
        True если бот может отвечать, False если бот должен молчать
        (бот выключен глобально, ожидание менеджера или cooldown)
    """
    # Проверяем глобальное состояние бота
    if not is_bot_enabled():
        return False
    
    if not chat_id:
        logger.warning("can_bot_reply called with empty chat_id")
        return True
    
    info = _sessions.get(chat_id)
    if not info:
        return True

    state = info.get("state")
    if state == SESSION_STATE_WAITING_MANAGER:
        return False

    if state == SESSION_STATE_COOLDOWN:
        until = info.get("until")
        if until and isinstance(until, datetime):
            if datetime.now() > until:
                clear_session(chat_id)
                return True
            return False
        # Если until не является datetime, очищаем некорректную сессию
        logger.warning("Invalid cooldown until value for chat_id=%s, clearing session", chat_id)
        clear_session(chat_id)
        return True

    return True


def get_session_info(chat_id: str) -> Optional[Dict[str, Any]]:
    """
    Получает информацию о сессии чата (для отладки).
    
    Args:
        chat_id: ID чата в Avito
        
    Returns:
        Словарь с информацией о сессии или None
    """
    return _sessions.get(chat_id)


def get_bot_mode() -> str:
    """
    Получает текущий режим работы бота.
    
    Returns:
        Режим работы бота: "listening", "partial" или "full"
    """
    return _bot_mode


def set_bot_mode(mode: str) -> None:
    """
    Устанавливает режим работы бота.
    
    Args:
        mode: Режим работы ("listening", "partial" или "full")
    """
    global _bot_mode
    if mode not in [BOT_MODE_LISTENING, BOT_MODE_PARTIAL, BOT_MODE_FULL]:
        logger.warning("Неизвестный режим бота: %s, используем 'full'", mode)
        mode = BOT_MODE_FULL
    
    _bot_mode = mode
    _save_bot_state()
    logger.info("Режим работы бота изменен: %s", mode)


def get_partial_percentage() -> int:
    """
    Получает процент сообщений для режима partial.
    
    Returns:
        Процент сообщений (0-100)
    """
    return _partial_percentage


def set_partial_percentage(percentage: int) -> None:
    """
    Устанавливает процент сообщений для режима partial.
    
    Args:
        percentage: Процент сообщений (0-100)
    """
    global _partial_percentage
    if percentage < 0:
        percentage = 0
    elif percentage > 100:
        percentage = 100
    
    _partial_percentage = percentage
    _save_bot_state()
    logger.info("Процент сообщений для режима partial установлен: %d%%", percentage)


def should_bot_reply(chat_id: str) -> bool:
    """
    Проверяет, должен ли бот отвечать на сообщение в указанном чате.
    Учитывает режим работы бота и процент для partial режима.
    
    Args:
        chat_id: ID чата в Avito
        
    Returns:
        True если бот должен отвечать, False если только слушать
    """
    # Если бот выключен - не отвечаем
    if not is_bot_enabled():
        return False
    
    # В режиме listening - только слушаем, не отвечаем
    if _bot_mode == BOT_MODE_LISTENING:
        return False
    
    # В режиме full - проверяем стандартные ограничения (cooldown, waiting_manager)
    if _bot_mode == BOT_MODE_FULL:
        return can_bot_reply(chat_id)
    
    # В режиме partial - проверяем процент и стандартные ограничения
    if _bot_mode == BOT_MODE_PARTIAL:
        if not can_bot_reply(chat_id):
            return False
        
        # Используем hash от chat_id для детерминированного выбора
        # Это гарантирует, что для одного чата решение будет одинаковым
        import hashlib
        chat_hash = int(hashlib.md5(chat_id.encode()).hexdigest(), 16)
        should_reply = (chat_hash % 100) < _partial_percentage
        return should_reply
    
    return False
