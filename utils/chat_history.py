"""
Утилиты для работы с историей чатов.

Предоставляет функции для безопасного сохранения сообщений в chat_history.json
с временными метками и информацией о токенах.
"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List

from config import CHAT_HISTORY_PATH
from responder import _load_json, _save_json

logger = logging.getLogger(__name__)

_DEDUP_WINDOW_SECONDS = 120


def _normalize_message_text(text: str) -> str:
    # Схлопываем пробелы/переводы строк, чтобы "Ок" и "Ок " считались одинаковыми
    return " ".join((text or "").split()).strip().lower()


def _is_recent_duplicate_of_last_message(
    dialog_history: List[Dict[str, Any]],
    content: str,
    *,
    last_roles: Optional[set] = None,
    window_seconds: int = _DEDUP_WINDOW_SECONDS,
) -> bool:
    """
    Дедуп для случаев, когда одно и то же сообщение сохраняется двумя путями:
    - менеджер ответил из Telegram -> роль "manager"
    - Avito webhook пришел тем же текстом -> роль "avito_owner"
    """
    if not dialog_history:
        return False

    norm_new = _normalize_message_text(content)
    if not norm_new:
        return False

    last_msg = dialog_history[-1] if isinstance(dialog_history[-1], dict) else None
    if not last_msg:
        return False

    last_role = str(last_msg.get("role") or "").strip()
    if last_roles is not None and last_role not in last_roles:
        return False

    last_content = _normalize_message_text(str(last_msg.get("content") or ""))
    if last_content != norm_new:
        return False

    # Если есть timestamp — ограничиваем дедуп коротким окном времени
    ts = last_msg.get("timestamp")
    if ts:
        try:
            last_time = datetime.fromisoformat(str(ts))
            now = datetime.now()
            delta = abs((now - last_time).total_seconds())
            return delta <= max(0, int(window_seconds))
        except Exception:
            # Если timestamp кривой — всё равно считаем дубликатом, раз совпал текст
            return True

    return True


def get_dialog_meta(dialog_id: str) -> Dict[str, Any]:
    """
    Возвращает метаданные диалога из chat_history.json["_meta"][dialog_id].
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        meta = (chat_history.get("_meta") or {}).get(dialog_id) or {}
        return meta if isinstance(meta, dict) else {}
    except Exception as e:
        logger.warning("Failed to load dialog meta for dialog_id=%s: %s", dialog_id, e)
        return {}


def set_dialog_meta(dialog_id: str, **fields: Any) -> None:
    """
    Устанавливает/обновляет метаданные диалога в chat_history.json["_meta"][dialog_id].
    """
    if not dialog_id:
        return
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        if not isinstance(chat_history, dict):
            chat_history = {}
        meta_root = chat_history.get("_meta")
        if not isinstance(meta_root, dict):
            meta_root = {}
            chat_history["_meta"] = meta_root
        meta = meta_root.get(dialog_id)
        if not isinstance(meta, dict):
            meta = {}
        for k, v in fields.items():
            if v is None:
                continue
            meta[k] = v
        meta_root[dialog_id] = meta
        _save_json(CHAT_HISTORY_PATH, chat_history)
    except Exception as e:
        logger.warning("Failed to set dialog meta for dialog_id=%s: %s", dialog_id, e)


def set_dialog_account_id(dialog_id: str, account_id: str) -> None:
    """
    Сохраняет account_id (user_id аккаунта Avito) для диалога avito_<chat_id>.
    """
    if not account_id:
        return
    set_dialog_meta(dialog_id, account_id=str(account_id).strip())


def save_user_message(dialog_id: str, content: str) -> None:
    """
    Сохраняет сообщение пользователя в историю чата.
    
    Args:
        dialog_id: ID диалога (например, "avito_123" или "tg_456")
        content: Текст сообщения пользователя
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        dialog_history = chat_history.get(dialog_id, [])
        
        dialog_history.append({
            "role": "user",
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        
        chat_history[dialog_id] = dialog_history
        
        # Убираем флаг processed, так как переписка возобновилась
        clear_dialog_processed_flag(dialog_id, chat_history)
        
        _save_json(CHAT_HISTORY_PATH, chat_history)
        
        logger.debug(
            "Saved user message to chat history for dialog_id=%s: %d messages",
            dialog_id, len(chat_history[dialog_id])
        )
    except Exception as e:
        logger.warning("Failed to save user message to chat history: %s", e)


def save_assistant_message(
    dialog_id: str,
    content: str,
    usage: Optional[Dict[str, Any]] = None
) -> None:
    """
    Сохраняет ответ ассистента в историю чата.
    
    Args:
        dialog_id: ID диалога (например, "avito_123" или "tg_456")
        content: Текст ответа ассистента
        usage: Информация об использовании токенов (опционально)
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        dialog_history = chat_history.get(dialog_id, [])

        # Если webhook уже успел сохранить это же исходящее сообщение как avito_owner — не плодим дубль.
        if _is_recent_duplicate_of_last_message(
            dialog_history,
            content,
            last_roles={"avito_owner", "assistant"},
        ):
            logger.debug(
                "Skipping duplicate assistant message for dialog_id=%s (same as last role=%s)",
                dialog_id,
                (dialog_history[-1] or {}).get("role") if dialog_history else None,
            )
            return
        
        assistant_entry = {
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().isoformat()
        }
        
        if usage:
            assistant_entry["usage"] = usage
        
        dialog_history.append(assistant_entry)
        chat_history[dialog_id] = dialog_history
        
        # Убираем флаг processed, так как переписка возобновилась
        clear_dialog_processed_flag(dialog_id, chat_history)
        
        _save_json(CHAT_HISTORY_PATH, chat_history)
        
        logger.debug(
            "Saved assistant message to chat history for dialog_id=%s: %d messages",
            dialog_id, len(chat_history[dialog_id])
        )
    except Exception as e:
        logger.warning("Failed to save assistant message to chat history: %s", e)


def save_manager_message(dialog_id: str, content: str) -> None:
    """
    Сохраняет ответ менеджера в историю чата.
    
    Args:
        dialog_id: ID диалога (например, "avito_123")
        content: Текст ответа менеджера
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        dialog_history = chat_history.get(dialog_id, [])

        # Если webhook уже успел сохранить это же сообщение как avito_owner — не плодим дубль.
        if _is_recent_duplicate_of_last_message(
            dialog_history,
            content,
            last_roles={"avito_owner", "manager"},
        ):
            logger.debug(
                "Skipping duplicate manager message for dialog_id=%s (same as last role=%s)",
                dialog_id,
                (dialog_history[-1] or {}).get("role") if dialog_history else None,
            )
            return
        
        dialog_history.append({
            "role": "manager",
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        
        chat_history[dialog_id] = dialog_history
        
        # Убираем флаг processed, так как переписка возобновилась
        clear_dialog_processed_flag(dialog_id, chat_history)
        
        _save_json(CHAT_HISTORY_PATH, chat_history)
        
        logger.debug(
            "Saved manager message to chat history for dialog_id=%s: %d messages",
            dialog_id, len(chat_history[dialog_id])
        )
    except Exception as e:
        logger.warning("Failed to save manager message to chat history: %s", e)


def save_avito_owner_message(dialog_id: str, content: str) -> None:
    """
    Сохраняет сообщение владельца аккаунта Avito в историю чата.
    
    Args:
        dialog_id: ID диалога (например, "avito_123")
        content: Текст сообщения владельца аккаунта
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        dialog_history = chat_history.get(dialog_id, [])

        # Ключевой кейс: менеджер ответил в Telegram (роль "manager"),
        # затем Avito прислал то же сообщение как "avito_owner".
        if _is_recent_duplicate_of_last_message(
            dialog_history,
            content,
            last_roles={"manager", "assistant", "avito_owner"},
        ):
            logger.debug(
                "Skipping duplicate avito_owner message for dialog_id=%s (same as last role=%s)",
                dialog_id,
                (dialog_history[-1] or {}).get("role") if dialog_history else None,
            )
            return
        
        dialog_history.append({
            "role": "avito_owner",
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        
        chat_history[dialog_id] = dialog_history
        
        # Убираем флаг processed, так как переписка возобновилась
        clear_dialog_processed_flag(dialog_id, chat_history)
        
        _save_json(CHAT_HISTORY_PATH, chat_history)
        
        logger.debug(
            "Saved avito_owner message to chat history for dialog_id=%s: %d messages",
            dialog_id, len(chat_history[dialog_id])
        )
    except Exception as e:
        logger.warning("Failed to save avito_owner message to chat history: %s", e)


def get_dialog_history(dialog_id: str) -> List[Dict[str, Any]]:
    """
    Получает историю диалога.
    
    Args:
        dialog_id: ID диалога
        
    Returns:
        Список сообщений диалога
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        return chat_history.get(dialog_id, [])
    except Exception as e:
        logger.warning("Failed to load dialog history for dialog_id=%s: %s", dialog_id, e)
        return []


def _get_dialog_meta(chat_history: Dict[str, Any], dialog_id: str) -> Dict[str, Any]:
    """
    Получает метаданные диалога.
    
    Args:
        chat_history: Загруженная история чатов
        dialog_id: ID диалога
        
    Returns:
        Словарь с метаданными диалога
    """
    if "_meta" not in chat_history:
        chat_history["_meta"] = {}
    if dialog_id not in chat_history["_meta"]:
        chat_history["_meta"][dialog_id] = {}
    return chat_history["_meta"][dialog_id]


def is_dialog_processed(dialog_id: str) -> bool:
    """
    Проверяет, был ли диалог обработан для формирования FAQ.
    
    Args:
        dialog_id: ID диалога
        
    Returns:
        True если диалог был обработан, False иначе
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        meta = _get_dialog_meta(chat_history, dialog_id)
        return meta.get("processed", False)
    except Exception as e:
        logger.warning("Failed to check if dialog is processed for dialog_id=%s: %s", dialog_id, e)
        return False


def mark_dialog_processed(dialog_id: str) -> None:
    """
    Отмечает диалог как обработанный для формирования FAQ.
    
    Args:
        dialog_id: ID диалога
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
        meta = _get_dialog_meta(chat_history, dialog_id)
        meta["processed"] = True
        meta["last_processed_at"] = datetime.now().isoformat()
        chat_history["_meta"][dialog_id] = meta
        _save_json(CHAT_HISTORY_PATH, chat_history)
        logger.debug("Marked dialog as processed: dialog_id=%s", dialog_id)
    except Exception as e:
        logger.warning("Failed to mark dialog as processed for dialog_id=%s: %s", dialog_id, e)


def clear_dialog_processed_flag(dialog_id: str, chat_history: Optional[Dict[str, Any]] = None) -> None:
    """
    Убирает флаг processed у диалога (когда переписка возобновилась).
    
    Args:
        dialog_id: ID диалога
        chat_history: Опционально, уже загруженная история (чтобы не загружать дважды)
                     Если передан, изменения не сохраняются автоматически (сохранение делается вызывающей функцией)
    """
    try:
        if chat_history is None:
            chat_history = _load_json(CHAT_HISTORY_PATH, {})
            save_after = True
        else:
            save_after = False
        
        if "_meta" not in chat_history:
            return  # Нет метаданных - нечего очищать
        
        if dialog_id not in chat_history["_meta"]:
            return  # Нет метаданных для этого диалога
        
        meta = chat_history["_meta"][dialog_id]
        if "processed" in meta:
            del meta["processed"]
        if "last_processed_at" in meta:
            del meta["last_processed_at"]
        
        # Если метаданные пустые, удаляем запись
        if not meta:
            del chat_history["_meta"][dialog_id]
        
        # Сохраняем только если мы загружали историю сами
        if save_after:
            _save_json(CHAT_HISTORY_PATH, chat_history)
        
        logger.debug("Cleared processed flag for dialog: dialog_id=%s", dialog_id)
    except Exception as e:
        logger.warning("Failed to clear processed flag for dialog_id=%s: %s", dialog_id, e)

