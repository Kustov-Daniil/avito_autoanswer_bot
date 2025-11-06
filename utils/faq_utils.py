"""
Утилиты для работы с FAQ.

Предоставляет функции для безопасной загрузки, сохранения и добавления записей в FAQ
с проверкой уникальности, валидацией и защитой от потери данных.
"""
import os
import json
import re
import shutil
import logging
from typing import Dict, Any, List, Tuple

from config import FAQ_PATH

logger = logging.getLogger(__name__)


def load_faq_safe() -> Tuple[List[Dict[str, Any]], int]:
    """
    Безопасно загружает FAQ с защитой от потери данных.
    
    Returns:
        Кортеж (список FAQ, количество записей)
    """
    from responder import _load_json
    
    backup_path = f"{FAQ_PATH}.backup"
    original_faq_count = 0
    current_faq = None
    
    try:
        with open(FAQ_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                logger.warning("FAQ файл пуст, пробуем восстановить из резервной копии")
                raise ValueError("FAQ файл пуст")
            current_faq = json.loads(content)
        # Проверяем, что это список
        if not isinstance(current_faq, list):
            logger.error("FAQ файл не содержит список, пробуем восстановить из резервной копии")
            raise ValueError("FAQ файл не содержит список")
        else:
            original_faq_count = len(current_faq)
            logger.debug("Загружен FAQ: %d записей", original_faq_count)
    except FileNotFoundError:
        logger.warning("FAQ файл не найден, пробуем восстановить из резервной копии")
        current_faq = None
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Ошибка при загрузке FAQ файла: %s, пробуем восстановить из резервной копии", e)
        # Пробуем исправить JSON (убрать лишние запятые в конце)
        try:
            with open(FAQ_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            # Убираем лишние запятые перед закрывающими скобками
            content = re.sub(r',\s*\]', ']', content)
            content = re.sub(r',\s*\}', '}', content)
            current_faq = json.loads(content)
            if isinstance(current_faq, list) and len(current_faq) > 0:
                original_faq_count = len(current_faq)
                logger.info("✅ FAQ исправлен автоматически: %d записей", original_faq_count)
                # Сохраняем исправленный FAQ
                with open(FAQ_PATH, "w", encoding="utf-8") as f:
                    json.dump(current_faq, f, ensure_ascii=False, indent=2)
            else:
                current_faq = None
        except Exception as fix_e:
            logger.warning("Не удалось автоматически исправить FAQ: %s", fix_e)
            current_faq = None
    except Exception as e:
        logger.exception("Неожиданная ошибка при загрузке FAQ: %s", e)
        current_faq = None
    
    # Если не удалось загрузить FAQ, пробуем восстановить из резервной копии
    if current_faq is None:
        if os.path.exists(backup_path):
            try:
                logger.info("Восстанавливаю FAQ из резервной копии: %s", backup_path)
                with open(backup_path, "r", encoding="utf-8") as f:
                    current_faq = json.load(f)
                if isinstance(current_faq, list):
                    original_faq_count = len(current_faq)
                    logger.info("✅ FAQ восстановлен из резервной копии: %d записей", original_faq_count)
                    # Восстанавливаем основной файл из резервной копии
                    shutil.copy2(backup_path, FAQ_PATH)
                else:
                    logger.error("Резервная копия FAQ не содержит список")
                    current_faq = []
            except Exception as restore_e:
                logger.exception("Не удалось восстановить FAQ из резервной копии: %s", restore_e)
                current_faq = []
        else:
            logger.warning("Резервная копия FAQ не найдена, создаем новый FAQ")
            current_faq = []
    
    if not isinstance(current_faq, list):
        logger.warning("FAQ данные не являются списком после всех попыток загрузки, возвращаем пустой список")
        current_faq = []
        original_faq_count = 0
    
    logger.debug("load_faq_safe возвращает: %d записей (original_count: %d)", len(current_faq), original_faq_count)
    return current_faq, original_faq_count


def save_faq_safe(faq_data: List[Dict[str, Any]], original_count: int) -> bool:
    """
    Безопасно сохраняет FAQ с проверкой целостности.
    
    Args:
        faq_data: Список FAQ записей для сохранения
        original_count: Оригинальное количество записей (для проверки)
        
    Returns:
        True если сохранение успешно, False иначе
    """
    backup_path = f"{FAQ_PATH}.backup"
    
    # Проверяем, что количество записей увеличилось (или не изменилось, если это обновление)
    if len(faq_data) < original_count:
        logger.error("⚠️ КРИТИЧЕСКАЯ ОШИБКА: Количество записей уменьшилось! "
                   "Было: %d, стало: %d", original_count, len(faq_data))
        return False
    
    # Создаем резервную копию перед сохранением
    try:
        if os.path.exists(FAQ_PATH):
            shutil.copy2(FAQ_PATH, backup_path)
            logger.debug("Создана резервная копия FAQ: %s", backup_path)
    except Exception as e:
        logger.warning("Не удалось создать резервную копию FAQ: %s", e)
    
    # Сохраняем обновленный FAQ атомарно
    try:
        os.makedirs(os.path.dirname(FAQ_PATH), exist_ok=True)
        # Используем временный файл для атомарной записи
        temp_path = f"{FAQ_PATH}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(faq_data, f, ensure_ascii=False, indent=2)
        # Атомарно заменяем файл
        os.replace(temp_path, FAQ_PATH)
        logger.info("✅ FAQ сохранен: %d записей (было: %d)", len(faq_data), original_count)
        return True
    except Exception as e:
        logger.exception("Ошибка при сохранении FAQ: %s", e)
        # Восстанавливаем из резервной копии
        if os.path.exists(backup_path):
            try:
                shutil.copy2(backup_path, FAQ_PATH)
                logger.info("Восстановлен FAQ из резервной копии после ошибки сохранения")
            except Exception as restore_e:
                logger.exception("Не удалось восстановить FAQ из резервной копии: %s", restore_e)
        return False


def validate_faq_entry(question: str, answer: str) -> Tuple[bool, str]:
    """
    Проверяет подходящесть FAQ записи.
    
    Args:
        question: Вопрос
        answer: Ответ
        
    Returns:
        Кортеж (валидна ли запись, сообщение об ошибке)
    """
    question = question.strip() if question else ""
    answer = answer.strip() if answer else ""
    
    if not question:
        return False, "Вопрос не может быть пустым"
    
    if not answer:
        return False, "Ответ не может быть пустым"
    
    # Минимальная длина вопроса (хотя бы 3 символа)
    if len(question) < 3:
        return False, "Вопрос слишком короткий (минимум 3 символа)"
    
    # Минимальная длина ответа (хотя бы 5 символов)
    if len(answer) < 5:
        return False, "Ответ слишком короткий (минимум 5 символов)"
    
    # Максимальная длина вопроса (не более 500 символов)
    if len(question) > 500:
        return False, "Вопрос слишком длинный (максимум 500 символов)"
    
    # Максимальная длина ответа (не более 2000 символов)
    if len(answer) > 2000:
        return False, "Ответ слишком длинный (максимум 2000 символов)"
    
    return True, ""


def add_faq_entry_safe(question: str, answer: str, source: str) -> Tuple[bool, str]:
    """
    Безопасно добавляет одну запись в FAQ с проверкой уникальности и валидацией.
    
    Args:
        question: Вопрос
        answer: Ответ
        source: Источник добавления ("admin", "manager", "user_like", "manager_like")
        
    Returns:
        Кортеж (успешно ли добавлено, сообщение)
    """
    # Валидация записи
    logger.debug("Валидация FAQ записи: вопрос='%s' (длина: %d), ответ='%s' (длина: %d), источник='%s'", 
                 question[:50], len(question), answer[:50], len(answer), source)
    is_valid, error_msg = validate_faq_entry(question, answer)
    if not is_valid:
        logger.warning("FAQ запись не прошла валидацию: %s (вопрос: '%s', ответ: '%s')", 
                       error_msg, question[:50], answer[:50])
        return False, error_msg
    
    # Загружаем FAQ
    logger.debug("Загрузка FAQ для добавления записи")
    current_faq, original_count = load_faq_safe()
    logger.debug("Загружен FAQ: %d записей", len(current_faq))
    
    # Проверяем уникальность вопроса (case-insensitive)
    question_lower = question.lower().strip()
    existing_questions = {
        item.get("question", "").lower().strip() 
        for item in current_faq 
        if item.get("question")
    }
    logger.debug("Проверка уникальности: вопрос='%s', существующих вопросов: %d", 
                 question_lower[:50], len(existing_questions))
    
    if question_lower in existing_questions:
        logger.info("Вопрос уже есть в FAQ, пропускаем добавление: '%s'", question[:50])
        return False, "Вопрос уже существует в FAQ"
    
    # Добавляем новую запись
    new_entry = {
        "question": question.strip(),
        "answer": answer.strip(),
        "source": source
    }
    current_faq.append(new_entry)
    
    # Сохраняем с проверкой
    if save_faq_safe(current_faq, original_count):
        logger.info("✅ FAQ запись добавлена: вопрос='%s', источник='%s', было: %d, стало: %d", 
                   question[:50], source, original_count, len(current_faq))
        return True, f"Запись успешно добавлена в FAQ (всего: {len(current_faq)} записей)"
    else:
        return False, "Ошибка при сохранении FAQ"


def add_faq_entries_batch(entries: List[Dict[str, str]], source: str) -> Tuple[int, int, List[str]]:
    """
    Безопасно добавляет несколько записей в FAQ с проверкой уникальности и валидацией.
    
    Args:
        entries: Список записей [{"question": str, "answer": str}, ...]
        source: Источник добавления ("admin", "manager", "user_like", "manager_like")
        
    Returns:
        Кортеж (количество добавленных, количество пропущенных, список ошибок)
    """
    if not entries:
        return 0, 0, []
    
    # Загружаем FAQ один раз
    current_faq, original_count = load_faq_safe()
    
    # Собираем существующие вопросы
    existing_questions = {
        item.get("question", "").lower().strip() 
        for item in current_faq 
        if item.get("question")
    }
    
    added_count = 0
    skipped_count = 0
    errors = []
    
    # Обрабатываем каждую запись
    for entry in entries:
        question = entry.get("question", "").strip()
        answer = entry.get("answer", "").strip()
        
        # Валидация
        is_valid, error_msg = validate_faq_entry(question, answer)
        if not is_valid:
            skipped_count += 1
            errors.append(f"'{question[:30]}...': {error_msg}")
            continue
        
        # Проверка уникальности
        question_lower = question.lower().strip()
        if question_lower in existing_questions:
            skipped_count += 1
            logger.debug("Пропускаем дубликат вопроса: %s", question[:50])
            continue
        
        # Добавляем запись
        new_entry = {
            "question": question,
            "answer": answer,
            "source": source
        }
        current_faq.append(new_entry)
        existing_questions.add(question_lower)
        added_count += 1
    
    # Сохраняем все добавленные записи одним разом
    if added_count > 0:
        if save_faq_safe(current_faq, original_count):
            logger.info("✅ Добавлено %d записей в FAQ (пропущено: %d), источник='%s', было: %d, стало: %d", 
                       added_count, skipped_count, source, original_count, len(current_faq))
        else:
            return 0, len(entries), ["Ошибка при сохранении FAQ"]
    
    return added_count, skipped_count, errors


def parse_faq_text(text: str) -> List[Dict[str, str]]:
    """
    Парсит текст в формате Q: ... A: ... или JSON в список FAQ записей.
    
    Args:
        text: Текст для парсинга
        
    Returns:
        Список FAQ записей [{"question": str, "answer": str}, ...]
    """
    if not text or not text.strip():
        return []
    
    text = text.strip()
    faq_entries = []
    
    # Пробуем парсить как JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and "question" in item and "answer" in item:
                    faq_entries.append({
                        "question": str(item["question"]).strip(),
                        "answer": str(item["answer"]).strip()
                    })
            if faq_entries:
                return faq_entries
    except json.JSONDecodeError:
        pass
    
    # Парсим формат Q: ... A: ...
    # Паттерн для поиска пар вопрос-ответ
    pattern = re.compile(r'Q:\s*(.+?)\s*A:\s*(.+?)(?=Q:|$)', re.IGNORECASE | re.DOTALL)
    matches = pattern.findall(text)
    
    for match in matches:
        question = match[0].strip()
        answer = match[1].strip()
        
        # Очищаем от лишних символов
        question = re.sub(r'^["\']|["\']$', '', question).strip()
        answer = re.sub(r'^["\']|["\']$', '', answer).strip()
        
        if question and answer:
            faq_entries.append({
                "question": question,
                "answer": answer
            })
    
    return faq_entries

