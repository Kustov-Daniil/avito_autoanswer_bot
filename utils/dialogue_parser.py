"""
Парсер файлов с переписками для извлечения знаний.

Поддерживает различные форматы:
- JSON (массив диалогов, объекты с сообщениями)
- TXT (текстовые переписки)
- CSV (таблицы с переписками)
- HTML (HTML файлы с переписками)
"""

import json
import csv
import re
import logging
from typing import List, Dict, Any, Optional
from io import StringIO
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def parse_dialogues_from_file(file_path: str, file_name: str) -> List[List[Dict[str, Any]]]:
    """
    Парсит файл с переписками и возвращает список диалогов.
    
    Каждый диалог - это список сообщений вида:
    [
        {"role": "user", "content": "текст сообщения"},
        {"role": "assistant", "content": "текст ответа"}
    ]
    
    Args:
        file_path: Путь к файлу
        file_name: Имя файла (для определения формата)
        
    Returns:
        Список диалогов (каждый диалог - список сообщений)
    """
    dialogues = []
    
    try:
        if file_name.endswith(".json"):
            dialogues = _parse_json_dialogues(file_path)
        elif file_name.endswith(".csv"):
            dialogues = _parse_csv_dialogues(file_path)
        elif file_name.endswith(".html"):
            dialogues = _parse_html_dialogues(file_path)
        elif file_name.endswith(".txt"):
            dialogues = _parse_txt_dialogues(file_path)
        else:
            # Пробуем определить формат по содержимому
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(1000)  # Первые 1000 символов
                if content.strip().startswith("["):
                    dialogues = _parse_json_dialogues(file_path)
                elif "," in content and "\n" in content:
                    dialogues = _parse_csv_dialogues(file_path)
                else:
                    dialogues = _parse_txt_dialogues(file_path)
    except Exception as e:
        logger.exception("Ошибка при парсинге файла %s: %s", file_path, e)
        raise
    
    return dialogues


def _parse_json_dialogues(file_path: str) -> List[List[Dict[str, Any]]]:
    """Парсит JSON файл с диалогами."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        data = json.load(f)
    
    dialogues = []
    
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                # Формат: {"messages": [...]} или {"dialogue": [...]}
                messages = item.get("messages") or item.get("dialogue") or item.get("history") or []
                if messages and isinstance(messages, list):
                    dialogue = _normalize_messages(messages)
                    if dialogue:
                        dialogues.append(dialogue)
                # Формат: прямой массив сообщений
                elif "role" in item and "content" in item:
                    dialogues.append([item])
            elif isinstance(item, list):
                # Формат: массив массивов сообщений
                dialogue = _normalize_messages(item)
                if dialogue:
                    dialogues.append(dialogue)
    elif isinstance(data, dict):
        # Формат: {"dialogues": [...]} или один диалог
        dialogues_list = data.get("dialogues") or data.get("messages") or data.get("history") or []
        if isinstance(dialogues_list, list):
            for item in dialogues_list:
                if isinstance(item, list):
                    dialogue = _normalize_messages(item)
                    if dialogue:
                        dialogues.append(dialogue)
    
    return dialogues


def _parse_csv_dialogues(file_path: str) -> List[List[Dict[str, Any]]]:
    """Парсит CSV файл с переписками."""
    dialogues = []
    current_dialogue = []
    current_dialogue_id = None
    
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        # Пробуем определить разделитель
        first_line = f.readline()
        f.seek(0)
        
        delimiter = ","
        if ";" in first_line:
            delimiter = ";"
        elif "\t" in first_line:
            delimiter = "\t"
        
        reader = csv.DictReader(f, delimiter=delimiter)
        
        for row in reader:
            # Ищем колонки с сообщениями и ролями
            message = None
            role = None
            dialogue_id = None
            
            # Пробуем найти колонки
            for key, value in row.items():
                key_lower = key.lower()
                if not value or not str(value).strip():
                    continue
                
                if "message" in key_lower or "text" in key_lower or "content" in key_lower:
                    message = str(value).strip()
                elif "role" in key_lower or "sender" in key_lower or "author" in key_lower:
                    role = _normalize_role(str(value).strip())
                elif "dialogue" in key_lower or "dialog" in key_lower or "chat" in key_lower:
                    dialogue_id = str(value).strip()
            
            if not message:
                continue
            
            if not role:
                # Пробуем определить роль по содержимому или другим колонкам
                role = "user"  # По умолчанию
            
            # Если есть dialogue_id и он изменился - начинаем новый диалог
            if dialogue_id and dialogue_id != current_dialogue_id:
                if current_dialogue:
                    dialogues.append(current_dialogue)
                current_dialogue = []
                current_dialogue_id = dialogue_id
            
            current_dialogue.append({
                "role": role,
                "content": message
            })
    
    # Добавляем последний диалог
    if current_dialogue:
        dialogues.append(current_dialogue)
    
    return dialogues


def _parse_html_dialogues(file_path: str) -> List[List[Dict[str, Any]]]:
    """Парсит HTML файл с переписками."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        html_content = f.read()
    
    soup = BeautifulSoup(html_content, "html.parser")
    dialogues = []
    current_dialogue = []
    
    # Ищем элементы с сообщениями (разные варианты структуры)
    # Вариант 1: div с классом message, msg, chat-message и т.д.
    message_elements = soup.find_all(["div", "li", "p"], class_=re.compile(r"message|msg|chat", re.I))
    
    if not message_elements:
        # Вариант 2: просто div с текстом
        message_elements = soup.find_all(["div", "p", "span"], string=re.compile(r".+"))
    
    for elem in message_elements:
        text = elem.get_text(strip=True)
        if not text or len(text) < 3:
            continue
        
        # Пробуем определить роль по классам или атрибутам
        role = "user"
        classes = elem.get("class", [])
        if any("bot" in str(c).lower() or "assistant" in str(c).lower() or "answer" in str(c).lower() for c in classes):
            role = "assistant"
        elif any("user" in str(c).lower() or "client" in str(c).lower() or "question" in str(c).lower() for c in classes):
            role = "user"
        
        current_dialogue.append({
            "role": role,
            "content": text
        })
    
    if current_dialogue:
        dialogues.append(current_dialogue)
    
    return dialogues


def _parse_txt_dialogues(file_path: str) -> List[List[Dict[str, Any]]]:
    """Парсит TXT файл с переписками."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    
    dialogues = []
    current_dialogue = []
    
    for line in lines:
        line = line.strip()
        if not line:
            # Пустая строка - возможный разделитель диалогов
            if current_dialogue:
                dialogues.append(current_dialogue)
                current_dialogue = []
            continue
        
        # Пробуем определить роль по префиксам
        role = "user"
        content = line
        
        # Паттерны для определения роли
        role_patterns = [
            (r"^(клиент|пользователь|user|клиентка):\s*", "user"),
            (r"^(консультант|менеджер|бот|assistant|bot|ответ):\s*", "assistant"),
            (r"^(вопрос|q|question):\s*", "user"),
            (r"^(ответ|a|answer|ответил):\s*", "assistant"),
        ]
        
        for pattern, detected_role in role_patterns:
            match = re.match(pattern, line, re.I)
            if match:
                role = detected_role
                content = line[match.end():].strip()
                break
        
        current_dialogue.append({
            "role": role,
            "content": content
        })
    
    # Добавляем последний диалог
    if current_dialogue:
        dialogues.append(current_dialogue)
    
    return dialogues


def _normalize_messages(messages: List[Any]) -> List[Dict[str, Any]]:
    """Нормализует список сообщений в стандартный формат."""
    normalized = []
    
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        
        role = msg.get("role") or msg.get("sender") or msg.get("author") or "user"
        content = msg.get("content") or msg.get("text") or msg.get("message") or ""
        
        if not content or not str(content).strip():
            continue
        
        normalized.append({
            "role": _normalize_role(str(role)),
            "content": str(content).strip()
        })
    
    return normalized


def _normalize_role(role: str) -> str:
    """Нормализует роль сообщения."""
    role_lower = role.lower().strip()
    
    if role_lower in ["user", "клиент", "пользователь", "client", "customer"]:
        return "user"
    elif role_lower in ["assistant", "bot", "бот", "консультант", "менеджер", "manager"]:
        return "assistant"
    elif role_lower in ["manager", "менеджер", "admin", "админ"]:
        return "manager"
    else:
        return "user"  # По умолчанию

