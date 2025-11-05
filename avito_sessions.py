"""
Управление сессиями чатов для контроля активности бота.

Модуль управляет состояниями чатов (waiting_manager, cooldown) для контроля,
когда бот должен отвечать автоматически, а когда ждать ответа менеджера.
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from config import COOLDOWN_MINUTES_AFTER_MANAGER
import logging

logger = logging.getLogger(__name__)

# Типы состояний
SESSION_STATE_WAITING_MANAGER: str = "waiting_manager"
SESSION_STATE_COOLDOWN: str = "cooldown"

# In-memory storage (для production можно заменить на Redis/БД)
_sessions: Dict[str, Dict[str, Any]] = {}  # chat_id -> {"state": str, "until": Optional[datetime]}


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
        (ожидание менеджера или cooldown)
    """
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
