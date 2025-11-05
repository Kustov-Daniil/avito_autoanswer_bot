"""
Утилиты для безопасной отправки сообщений в Telegram с обработкой rate limiting.
"""
import asyncio
import logging
from typing import Optional
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError
from aiogram.types import Message
from aiogram import Bot

logger = logging.getLogger(__name__)

# Константы для retry логики
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_DELAY_ON_ERROR: float = 1.0
RETRY_DELAY_BUFFER: float = 0.5  # Дополнительная задержка после rate limit


async def safe_send_message(
    message: Message,
    text: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    delay_on_error: float = DEFAULT_DELAY_ON_ERROR
) -> Optional[Message]:
    """
    Безопасная отправка сообщения с обработкой rate limiting.
    
    Args:
        message: Объект сообщения для reply
        text: Текст для отправки
        max_retries: Максимальное количество попыток
        delay_on_error: Задержка при ошибке (в секундах)
    
    Returns:
        Message объект при успехе, None при неудаче
    """
    if not text or not text.strip():
        logger.warning("Attempted to send empty message")
        return None
    
    for attempt in range(max_retries):
        try:
            return await message.reply(text)
        except TelegramRetryAfter as e:
            retry_after = e.retry_after
            logger.warning(
                "Rate limit exceeded. Retry after %s seconds. Attempt %d/%d",
                retry_after, attempt + 1, max_retries
            )
            
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_after + RETRY_DELAY_BUFFER)
            else:
                logger.error(
                    "Failed to send message after %d attempts due to rate limiting",
                    max_retries
                )
                return None
                
        except TelegramAPIError as e:
            logger.error("Telegram API error: %s", e)
            if attempt < max_retries - 1:
                # Exponential backoff
                await asyncio.sleep(delay_on_error * (attempt + 1))
            else:
                return None
                
        except Exception as e:
            logger.exception("Unexpected error sending message: %s", e)
            if attempt < max_retries - 1:
                await asyncio.sleep(delay_on_error * (attempt + 1))
            else:
                return None
    
    return None


async def safe_send_message_to_chat(
    bot: Bot,
    chat_id: int,
    text: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    delay_on_error: float = DEFAULT_DELAY_ON_ERROR
) -> bool:
    """
    Безопасная отправка сообщения в чат по chat_id с обработкой rate limiting.
    
    Args:
        bot: Объект бота
        chat_id: ID чата
        text: Текст для отправки
        max_retries: Максимальное количество попыток
        delay_on_error: Задержка при ошибке (в секундах)
    
    Returns:
        True при успехе, False при неудаче
    """
    if not text or not text.strip():
        logger.warning("Attempted to send empty message to chat %d", chat_id)
        return False
    
    if not chat_id:
        logger.error("Invalid chat_id: %s", chat_id)
        return False
    
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id, text)
            return True
        except TelegramRetryAfter as e:
            retry_after = e.retry_after
            logger.warning(
                "Rate limit exceeded for chat %d. Retry after %s seconds. Attempt %d/%d",
                chat_id, retry_after, attempt + 1, max_retries
            )
            
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_after + RETRY_DELAY_BUFFER)
            else:
                logger.error(
                    "Failed to send message to chat %d after %d attempts due to rate limiting",
                    chat_id, max_retries
                )
                return False
                
        except TelegramAPIError as e:
            logger.error("Telegram API error for chat %d: %s", chat_id, e)
            if attempt < max_retries - 1:
                # Exponential backoff
                await asyncio.sleep(delay_on_error * (attempt + 1))
            else:
                return False
                
        except Exception as e:
            logger.exception("Unexpected error sending message to chat %d: %s", chat_id, e)
            if attempt < max_retries - 1:
                await asyncio.sleep(delay_on_error * (attempt + 1))
            else:
                return False
    
    return False
