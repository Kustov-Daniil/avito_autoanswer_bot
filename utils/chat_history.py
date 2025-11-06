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
        
        assistant_entry = {
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().isoformat()
        }
        
        if usage:
            assistant_entry["usage"] = usage
        
        dialog_history.append(assistant_entry)
        chat_history[dialog_id] = dialog_history
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
        
        dialog_history.append({
            "role": "manager",
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        
        chat_history[dialog_id] = dialog_history
        _save_json(CHAT_HISTORY_PATH, chat_history)
        
        logger.debug(
            "Saved manager message to chat history for dialog_id=%s: %d messages",
            dialog_id, len(chat_history[dialog_id])
        )
    except Exception as e:
        logger.warning("Failed to save manager message to chat history: %s", e)


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

