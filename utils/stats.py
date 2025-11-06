"""
Утилиты для расчета статистики работы бота.

Предоставляет функции для расчета статистики по чатам, ответам, токенам,
времени ответа менеджера и экономическим метрикам.
"""
import logging
from datetime import datetime
from typing import Dict, Any, List

from config import (
    CHAT_HISTORY_PATH, FAQ_PATH, SIGNAL_PHRASES,
    MANAGER_COST_PER_HOUR, USD_RATE
)
from responder import _load_json
from utils.faq_utils import load_faq_safe

logger = logging.getLogger(__name__)


def calculate_token_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """
    Рассчитывает стоимость использования токенов для указанной модели.
    
    Args:
        model: Название модели (например, "gpt-4o", "gpt-5", "gpt-5-mini")
        prompt_tokens: Количество токенов в промпте
        completion_tokens: Количество токенов в ответе
        
    Returns:
        Стоимость в долларах США
    """
    # Цены на модели OpenAI (на 1M токенов)
    # Источник: https://openai.com/api/pricing/ (примерные цены, нужно обновлять)
    pricing = {
        "gpt-4o": {
            "input": 2.50,   # $2.50 за 1M input tokens
            "output": 10.00  # $10.00 за 1M output tokens
        },
        "gpt-5": {
            "input": 2.50,   # Предположительно похожие цены
            "output": 10.00
        },
        "gpt-5-mini": {
            "input": 0.15,   # $0.15 за 1M input tokens (дешевле)
            "output": 0.60   # $0.60 за 1M output tokens
        }
    }
    
    # Получаем цены для модели или используем значения по умолчанию
    model_pricing = pricing.get(model, pricing["gpt-4o"])
    
    # Рассчитываем стоимость
    input_cost = (prompt_tokens / 1_000_000) * model_pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * model_pricing["output"]
    
    return input_cost + output_cost


def calculate_stats() -> Dict[str, Any]:
    """
    Вычисляет статистику работы бота на основе chat_history.json и FAQ.
    
    Returns:
        Словарь со статистикой:
        - total_chats: количество чатов Avito, в которых отвечал бот или менеджер
        - total_bot_responses: количество ответов бота (по role="assistant")
        - total_manager_responses: количество ответов менеджера (по role="manager")
        - total_responses: общее количество ответов (бот + менеджер)
        - bot_response_rate: доля ответов бота от всех ответов (%)
        - manager_response_rate: доля ответов менеджера от всех ответов (%)
        - manager_transfers: количество ответов бота, которые перешли на менеджера
        - manager_transfer_rate: доля ответов бота, перешедших на менеджера (%)
        - bot_finished_dialogs: количество диалогов, завершенных разговором с ботом
        - manager_finished_dialogs: количество диалогов, завершенных разговором с менеджером
        - bot_finish_rate: доля диалогов, завершенных разговором с ботом (%)
        - manager_finish_rate: доля диалогов, завершенных разговором с менеджером (%)
        - faq_total: общее количество вопросов в FAQ
        - faq_admin: количество вопросов, добавленных админом
        - faq_manager: количество вопросов, добавленных менеджером
        - faq_manager_like: количество вопросов, лайкнутых менеджером
        - total_prompt_tokens: общее количество токенов в промптах
        - total_completion_tokens: общее количество токенов в ответах
        - total_tokens: общее количество токенов
        - total_cost_usd: общая стоимость использования LLM в долларах
        - total_cost_rub: общая стоимость использования LLM в рублях
        - avg_manager_response_time_seconds: среднее время ответа менеджера в секундах
        - avg_manager_response_time_hours: среднее время ответа менеджера в часах
        - saved_time_hours: сэкономленное время менеджера в часах
        - saved_money_rub: сэкономленные деньги менеджера в рублях
        - net_savings_rub: чистая экономия в рублях
    """
    try:
        chat_history = _load_json(CHAT_HISTORY_PATH, {})
    except Exception as e:
        logger.exception("Ошибка при загрузке chat_history для статистики: %s", e)
        chat_history = {}
    
    # Загружаем FAQ для статистики (используем безопасную загрузку)
    try:
        faq_data, _ = load_faq_safe()
        if not isinstance(faq_data, list):
            logger.warning("FAQ данные не являются списком, используем пустой список")
            faq_data = []
    except Exception as e:
        logger.exception("Ошибка при загрузке FAQ для статистики: %s", e)
        faq_data = []
    
    total_chats = 0
    total_bot_responses = 0
    total_manager_responses = 0
    manager_transfers = 0
    bot_finished_dialogs = 0
    manager_finished_dialogs = 0
    
    # Статистика токенов
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    total_cost_usd = 0.0
    
    # Статистика времени ответа менеджера
    manager_response_times: List[float] = []  # Список времен ответа в секундах
    
    # Сигнальная фраза, которая заменяет ответ при переходе на менеджера
    manager_signal_phrase = "Подождите, пожалуйста, уточняю информацию"
    
    # Обрабатываем только чаты Avito (начинаются с "avito_")
    for dialog_id, messages in chat_history.items():
        if not dialog_id.startswith("avito_"):
            continue
        
        if not isinstance(messages, list):
            continue
        
        # Подсчитываем ответы бота и менеджера в этом чате
        bot_responses_in_chat = 0
        manager_responses_in_chat = 0
        
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            
            role = msg.get("role", "")
            content = msg.get("content", "").strip()
            
            # Считаем ответы по полю role
            if role == "assistant" and content:
                bot_responses_in_chat += 1
                total_bot_responses += 1
                
                # Подсчитываем токены, если есть информация об использовании
                usage = msg.get("usage", {})
                if isinstance(usage, dict):
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    model = usage.get("model", "gpt-4o")
                    
                    if prompt_tokens > 0 or completion_tokens > 0:
                        total_prompt_tokens += prompt_tokens
                        total_completion_tokens += completion_tokens
                        total_tokens += prompt_tokens + completion_tokens
                        
                        # Рассчитываем стоимость в долларах
                        cost_usd = calculate_token_cost(model, prompt_tokens, completion_tokens)
                        total_cost_usd += cost_usd
                
                # Проверяем, является ли это переходом на менеджера
                content_lower = content.lower()
                is_manager_transfer = (
                    manager_signal_phrase.lower() in content_lower or
                    any(phrase.lower() in content_lower for phrase in SIGNAL_PHRASES)
                )
                
                if is_manager_transfer:
                    manager_transfers += 1
            elif role == "manager" and content:
                manager_responses_in_chat += 1
                total_manager_responses += 1
                
                # Рассчитываем время ответа менеджера
                manager_timestamp = msg.get("timestamp")
                if manager_timestamp:
                    try:
                        manager_time = datetime.fromisoformat(manager_timestamp)
                        
                        # Ищем предыдущее сообщение пользователя или бота (которое могло вызвать ответ менеджера)
                        msg_index = messages.index(msg)
                        for prev_msg in reversed(messages[:msg_index]):
                            if isinstance(prev_msg, dict):
                                prev_role = prev_msg.get("role", "")
                                prev_timestamp = prev_msg.get("timestamp")
                                if prev_timestamp and prev_role in ["user", "assistant"]:
                                    try:
                                        prev_time = datetime.fromisoformat(prev_timestamp)
                                        response_time_seconds = (manager_time - prev_time).total_seconds()
                                        if response_time_seconds > 0 and response_time_seconds < 86400:  # Меньше суток
                                            manager_response_times.append(response_time_seconds)
                                        break
                                    except (ValueError, TypeError):
                                        continue
                    except (ValueError, TypeError):
                        pass
        
        # Если бот или менеджер отвечали в этом чате, считаем чат
        if bot_responses_in_chat > 0 or manager_responses_in_chat > 0:
            total_chats += 1
            
            # Определяем, как завершился диалог
            if messages:
                last_msg = messages[-1]
                if isinstance(last_msg, dict):
                    last_role = last_msg.get("role", "")
                    if last_role == "manager":
                        manager_finished_dialogs += 1
                    elif last_role == "assistant":
                        last_content = last_msg.get("content", "").strip().lower()
                        is_manager_finish = (
                            manager_signal_phrase.lower() in last_content or
                            any(phrase.lower() in last_content for phrase in SIGNAL_PHRASES)
                        )
                        
                        if is_manager_finish:
                            manager_finished_dialogs += 1
                        else:
                            bot_finished_dialogs += 1
    
    # Подсчитываем статистику FAQ
    faq_total = 0
    faq_admin = 0
    faq_manager = 0
    faq_manager_like = 0
    
    if isinstance(faq_data, list):
        for item in faq_data:
            if isinstance(item, dict):
                faq_total += 1
                source = item.get("source", "")
                if source == "admin":
                    faq_admin += 1
                elif source == "manager":
                    faq_manager += 1
                elif source == "manager_like" or source == "user_like":
                    # Считаем user_like как manager_like (лайкнуто менеджером)
                    faq_manager_like += 1
    
    # Вычисляем общее количество ответов
    total_responses = total_bot_responses + total_manager_responses
    
    # Вычисляем доли
    bot_response_rate = (total_bot_responses / total_responses * 100) if total_responses > 0 else 0.0
    manager_response_rate = (total_manager_responses / total_responses * 100) if total_responses > 0 else 0.0
    manager_transfer_rate = (manager_transfers / total_bot_responses * 100) if total_bot_responses > 0 else 0.0
    total_finished_dialogs = bot_finished_dialogs + manager_finished_dialogs
    bot_finish_rate = (bot_finished_dialogs / total_finished_dialogs * 100) if total_finished_dialogs > 0 else 0.0
    manager_finish_rate = (manager_finished_dialogs / total_finished_dialogs * 100) if total_finished_dialogs > 0 else 0.0
    
    # Рассчитываем среднее время ответа менеджера
    avg_manager_response_time_seconds = 0.0
    if manager_response_times:
        avg_manager_response_time_seconds = sum(manager_response_times) / len(manager_response_times)
    avg_manager_response_time_hours = avg_manager_response_time_seconds / 3600
    
    # Рассчитываем стоимость бота в рублях
    total_cost_rub = total_cost_usd * USD_RATE
    
    # Рассчитываем сэкономленное время менеджера
    # Предполагаем, что каждый ответ бота экономит время менеджера (среднее время ответа менеджера)
    # Но учитываем только те ответы бота, которые не перешли на менеджера
    bot_responses_without_transfer = total_bot_responses - manager_transfers
    saved_time_hours = bot_responses_without_transfer * avg_manager_response_time_hours if avg_manager_response_time_hours > 0 else 0.0
    
    # Рассчитываем сэкономленные деньги менеджера
    saved_money_rub = saved_time_hours * MANAGER_COST_PER_HOUR
    
    # Чистая экономия (сэкономленные деньги минус стоимость бота)
    net_savings_rub = saved_money_rub - total_cost_rub
    
    return {
        "total_chats": total_chats,
        "total_bot_responses": total_bot_responses,
        "total_manager_responses": total_manager_responses,
        "total_responses": total_responses,
        "bot_response_rate": bot_response_rate,
        "manager_response_rate": manager_response_rate,
        "manager_transfers": manager_transfers,
        "manager_transfer_rate": manager_transfer_rate,
        "bot_finished_dialogs": bot_finished_dialogs,
        "manager_finished_dialogs": manager_finished_dialogs,
        "bot_finish_rate": bot_finish_rate,
        "manager_finish_rate": manager_finish_rate,
        "faq_total": faq_total,
        "faq_admin": faq_admin,
        "faq_manager": faq_manager,
        "faq_manager_like": faq_manager_like,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost_usd,
        "total_cost_rub": total_cost_rub,
        "avg_manager_response_time_seconds": avg_manager_response_time_seconds,
        "avg_manager_response_time_hours": avg_manager_response_time_hours,
        "saved_time_hours": saved_time_hours,
        "saved_money_rub": saved_money_rub,
        "net_savings_rub": net_savings_rub
    }

