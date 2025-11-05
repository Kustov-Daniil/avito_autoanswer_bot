#!/usr/bin/env python3
"""
Утилита для получения AVITO_ACCOUNT_ID из API Avito.

Этот скрипт поможет найти ваш account_id (user_id) для использования в .env файле.
"""

import requests
import logging
from config import AVITO_CLIENT_ID, AVITO_CLIENT_SECRET

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.avito.ru/token"
API_BASE_V2 = "https://api.avito.ru/messenger/v2/accounts"


def get_token():
    """Получает токен доступа к API Avito."""
    if not AVITO_CLIENT_ID or not AVITO_CLIENT_SECRET:
        logger.error("AVITO_CLIENT_ID или AVITO_CLIENT_SECRET не установлены в .env файле")
        return None
    
    data = {
        "grant_type": "client_credentials",
        "client_id": AVITO_CLIENT_ID,
        "client_secret": AVITO_CLIENT_SECRET,
        "scope": "messenger:read messenger:write"
    }
    
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=15)
        r.raise_for_status()
        j = r.json()
        return j.get("access_token")
    except Exception as e:
        logger.error("Ошибка при получении токена: %s", e)
        return None


def get_account_id_from_chats():
    """
    Пытается получить account_id из списка чатов.
    
    Примечание: Для этого нужен хотя бы один существующий чат.
    """
    token = get_token()
    if not token:
        return None
    
    # Пробуем разные возможные account_id (если они известны)
    # Или используем первый доступный
    
    # Попробуем получить информацию из токена или из других источников
    # К сожалению, API Avito не предоставляет прямой способ получить account_id
    # без знания хотя бы одного чата
    
    logger.info("Попытка получить account_id из списка чатов...")
    logger.warning("⚠️  Для получения account_id нужно знать хотя бы один chat_id")
    logger.warning("    или использовать ID из личного кабинета Avito")
    
    return None


def main():
    """Основная функция."""
    print("\n" + "="*60)
    print("Утилита для получения AVITO_ACCOUNT_ID")
    print("="*60 + "\n")
    
    if not AVITO_CLIENT_ID or not AVITO_CLIENT_SECRET:
        print("❌ Ошибка: AVITO_CLIENT_ID или AVITO_CLIENT_SECRET не установлены")
        print("\nПожалуйста, добавьте в .env файл:")
        print("  AVITO_CLIENT_ID=your_client_id")
        print("  AVITO_CLIENT_SECRET=your_client_secret")
        return
    
    print("✓ AVITO_CLIENT_ID и AVITO_CLIENT_SECRET установлены\n")
    
    token = get_token()
    if not token:
        print("❌ Не удалось получить токен доступа")
        return
    
    print("✓ Токен доступа получен\n")
    
    print("="*60)
    print("Как найти AVITO_ACCOUNT_ID:")
    print("="*60)
    print("\n1. Способ: Из личного кабинета Avito")
    print("   - Зайдите в личный кабинет Avito")
    print("   - Перейдите в раздел 'Настройки' -> 'API'")
    print("   - Найдите ваш 'user_id' или 'account_id'")
    print("\n2. Способ: Из webhook payload")
    print("   - Когда приходит webhook от Avito, проверьте структуру данных")
    print("   - account_id может быть в поле 'user_id' или 'account_id'")
    print("   - Или используйте 'author_id' из сообщения (если это ваш ID)")
    print("\n3. Способ: Попробовать известный ID")
    print("   - Если у вас есть чат, попробуйте использовать ID из URL")
    print("   - Обычно это числовой ID вашего аккаунта компании")
    print("\n" + "="*60)
    print("\nПосле того как вы найдете account_id, добавьте в .env файл:")
    print("  AVITO_ACCOUNT_ID=your_account_id_here")
    print("\n" + "="*60 + "\n")


if __name__ == "__main__":
    main()

