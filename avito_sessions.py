from datetime import datetime, timedelta
from config import COOLDOWN_MINUTES_AFTER_MANAGER

# In-memory (для production можно заменить на Redis/БД)
_sessions = {}  # chat_id -> {"state": "waiting_manager"|"cooldown", "until": datetime|None}


def set_waiting_manager(chat_id: str):
    """
    Включаем бесконечную паузу до первого ответа менеджера.
    """
    _sessions[chat_id] = {"state": "waiting_manager", "until": None}


def set_cooldown_after_manager(chat_id: str, minutes: int | None = None):
    """
    После ответа менеджера — пауза (по умолчанию 15 минут), затем бот снова активен.
    """
    mins = minutes if minutes is not None else COOLDOWN_MINUTES_AFTER_MANAGER
    _sessions[chat_id] = {
        "state": "cooldown",
        "until": datetime.now() + timedelta(minutes=mins),
    }


def clear_session(chat_id: str):
    _sessions.pop(chat_id, None)


def can_bot_reply(chat_id: str) -> bool:
    """
    True — бот может отвечать.
    False — бот молчит (ожидание менеджера или cooldown).
    """
    info = _sessions.get(chat_id)
    if not info:
        return True

    state = info.get("state")
    if state == "waiting_manager":
        return False

    if state == "cooldown":
        until = info.get("until")
        if until and datetime.now() > until:
            clear_session(chat_id)
            return True
        return False

    return True
