"""
CLI утилита для управления Avito Messenger API.

Предоставляет команды для:
- Подписки/отписки на webhook
- Просмотра списка чатов и сообщений
- Отправки сообщений в чаты
"""
import argparse
import sys
from typing import Any, List, Optional
from config import WEBHOOK_URL
from avito_api import (
    subscribe_webhook, unsubscribe_webhook, get_subscriptions,
    list_chats, get_chat, list_messages_v3, mark_chat_read,
    send_text_message, upload_image, send_image_message
)


def cmd_subscribe(args: argparse.Namespace) -> None:
    """
    Подписывается на webhook уведомления от Avito.
    
    Args:
        args: Аргументы командной строки с URL
    """
    url = args.url or WEBHOOK_URL
    if not url:
        print("WEBHOOK_URL пуст. Укажи --url или заполни PUBLIC_BASE_URL в .env", file=sys.stderr)
        sys.exit(1)
    
    ok = subscribe_webhook(url)
    print("Subscribed:", ok)
    sys.exit(0 if ok else 1)


def cmd_unsubscribe(args: argparse.Namespace) -> None:
    """
    Отписывается от webhook уведомлений.
    
    Args:
        args: Аргументы командной строки с URL
    """
    url = args.url or WEBHOOK_URL
    if not url:
        print("WEBHOOK_URL пуст. Укажи --url или заполни PUBLIC_BASE_URL в .env", file=sys.stderr)
        sys.exit(1)
    
    ok = unsubscribe_webhook(url)
    print("Unsubscribed:", ok)
    sys.exit(0 if ok else 1)


def cmd_subs(_args: argparse.Namespace) -> None:
    """
    Получает список активных подписок на webhook.
    
    Args:
        _args: Аргументы командной строки (не используются)
    """
    try:
        data = get_subscriptions()
        import json
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_chats(args: argparse.Namespace) -> None:
    """
    Получает список чатов.
    
    Args:
        args: Аргументы командной строки с параметрами фильтрации
    """
    try:
        data = list_chats(
            limit=args.limit,
            offset=args.offset,
            unread_only=args.unread_only,
            chat_types=args.types
        )
        import json
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_chat(args: argparse.Namespace) -> None:
    """
    Получает информацию о конкретном чате.
    
    Args:
        args: Аргументы командной строки с chat_id
    """
    try:
        data = get_chat(args.chat_id)
        import json
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_msgs(args: argparse.Namespace) -> None:
    """
    Получает список сообщений из чата.
    
    Args:
        args: Аргументы командной строки с chat_id и параметрами пагинации
    """
    try:
        items = list_messages_v3(args.chat_id, limit=args.limit, offset=args.offset)
        import json
        print(json.dumps(items, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_read(args: argparse.Namespace) -> None:
    """
    Отмечает чат как прочитанный.
    
    Args:
        args: Аргументы командной строки с chat_id
    """
    ok = mark_chat_read(args.chat_id)
    print("Marked read:", ok)
    sys.exit(0 if ok else 1)


def cmd_send_text(args: argparse.Namespace) -> None:
    """
    Отправляет текстовое сообщение в чат.
    
    Args:
        args: Аргументы командной строки с chat_id и text
    """
    ok = send_text_message(args.chat_id, args.text)
    print("Sent:", ok)
    sys.exit(0 if ok else 1)


def cmd_send_image(args: argparse.Namespace) -> None:
    """
    Отправляет изображение в чат.
    
    Args:
        args: Аргументы командной строки с chat_id и file
    """
    img_id = upload_image(args.file)
    if not img_id:
        print("upload failed", file=sys.stderr)
        sys.exit(2)
    
    ok = send_image_message(args.chat_id, img_id)
    print("Sent image:", ok, "image_id:", img_id)
    sys.exit(0 if ok else 1)


def main() -> None:
    """Основная функция CLI утилиты."""
    parser = argparse.ArgumentParser(description="Avito Messenger CLI")
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    
    # Webhook команды
    sp = subparsers.add_parser("subscribe")
    sp.add_argument("--url", help="Webhook URL")
    sp.set_defaults(func=cmd_subscribe)
    
    sp = subparsers.add_parser("unsubscribe")
    sp.add_argument("--url", help="Webhook URL")
    sp.set_defaults(func=cmd_unsubscribe)
    
    sp = subparsers.add_parser("subs")
    sp.set_defaults(func=cmd_subs)
    
    # Команды для работы с чатами
    sp = subparsers.add_parser("chats")
    sp.add_argument("--limit", type=int, default=20, help="Количество чатов")
    sp.add_argument("--offset", type=int, default=0, help="Сдвиг для пагинации")
    sp.add_argument("--unread-only", action="store_true", help="Только непрочитанные")
    sp.add_argument("--types", nargs="*", default=None, help="Типы чатов (u2i u2u)")
    sp.set_defaults(func=cmd_chats)
    
    sp = subparsers.add_parser("chat")
    sp.add_argument("chat_id", help="ID чата")
    sp.set_defaults(func=cmd_chat)
    
    sp = subparsers.add_parser("msgs")
    sp.add_argument("chat_id", help="ID чата")
    sp.add_argument("--limit", type=int, default=20, help="Количество сообщений")
    sp.add_argument("--offset", type=int, default=0, help="Сдвиг для пагинации")
    sp.set_defaults(func=cmd_msgs)
    
    sp = subparsers.add_parser("read")
    sp.add_argument("chat_id", help="ID чата")
    sp.set_defaults(func=cmd_read)
    
    # Команды для отправки сообщений
    sp = subparsers.add_parser("send-text")
    sp.add_argument("chat_id", help="ID чата")
    sp.add_argument("text", help="Текст сообщения")
    sp.set_defaults(func=cmd_send_text)
    
    sp = subparsers.add_parser("send-image")
    sp.add_argument("chat_id", help="ID чата")
    sp.add_argument("file", help="Путь к файлу изображения")
    sp.set_defaults(func=cmd_send_image)
    
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
