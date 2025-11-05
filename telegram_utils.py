"""
Утилиты для безопасной отправки сообщений в Telegram с обработкой rate limiting.
"""
import asyncio
import logging
from typing import Optional
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError
from aiogram.types import Message

logger = logging.getLogger(__name__)


async def safe_send_message(
    message: Message,
    text: str,
    max_retries: int = 3,
    delay_on_error: float = 1.0
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
    for attempt in range(max_retries):
        try:
            return await message.reply(text)
        except TelegramRetryAfter as e:
            retry_after = e.retry_after
            logger.warning(
                f"Rate limit exceeded. Retry after {retry_after} seconds. "
                f"Attempt {attempt + 1}/{max_retries}"
            )
            
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_after + 0.5)  # Небольшая дополнительная задержка
            else:
                logger.error(f"Failed to send message after {max_retries} attempts due to rate limiting")
                return None
                
        except TelegramAPIError as e:
            logger.error(f"Telegram API error: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay_on_error * (attempt + 1))  # Exponential backoff
            else:
                return None
                
        except Exception as e:
            logger.exception(f"Unexpected error sending message: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay_on_error * (attempt + 1))
            else:
                return None
    
    return None


async def safe_send_message_to_chat(
    bot,
    chat_id: int,
    text: str,
    max_retries: int = 3,
    delay_on_error: float = 1.0
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
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id, text)
            return True
        except TelegramRetryAfter as e:
            retry_after = e.retry_after
            logger.warning(
                f"Rate limit exceeded for chat {chat_id}. "
                f"Retry after {retry_after} seconds. Attempt {attempt + 1}/{max_retries}"
            )
            
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_after + 0.5)
            else:
                logger.error(
                    f"Failed to send message to chat {chat_id} after {max_retries} attempts "
                    f"due to rate limiting"
                )
                return False
                
        except TelegramAPIError as e:
            logger.error(f"Telegram API error for chat {chat_id}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay_on_error * (attempt + 1))
            else:
                return False
                
        except Exception as e:
            logger.exception(f"Unexpected error sending message to chat {chat_id}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay_on_error * (attempt + 1))
            else:
                return False
    
    return False

