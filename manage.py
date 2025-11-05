# manage.py
import argparse
import sys
from config import WEBHOOK_URL
from avito_api import (
    subscribe_webhook, unsubscribe_webhook, get_subscriptions,
    list_chats, get_chat, list_messages_v3, mark_chat_read,
    send_text_message, upload_image, send_image_message
)

def cmd_subscribe(args):
    url = args.url or WEBHOOK_URL
    if not url:
        print("WEBHOOK_URL пуст. Укажи --url или заполни PUBLIC_BASE_URL в .env", file=sys.stderr)
        sys.exit(1)
    ok = subscribe_webhook(url)
    print("Subscribed:", ok)

def cmd_unsubscribe(args):
    url = args.url or WEBHOOK_URL
    if not url:
        print("WEBHOOK_URL пуст. Укажи --url или заполни PUBLIC_BASE_URL в .env", file=sys.stderr)
        sys.exit(1)
    ok = unsubscribe_webhook(url)
    print("Unsubscribed:", ok)

def cmd_subs(_):
    print(get_subscriptions())

def cmd_chats(args):
    data = list_chats(limit=args.limit, offset=args.offset, unread_only=args.unread_only, chat_types=args.types)
    print(data)

def cmd_chat(args):
    print(get_chat(args.chat_id))

def cmd_msgs(args):
    items = list_messages_v3(args.chat_id, limit=args.limit, offset=args.offset)
    print(items)

def cmd_read(args):
    ok = mark_chat_read(args.chat_id)
    print("Marked read:", ok)

def cmd_send_text(args):
    ok = send_text_message(args.chat_id, args.text)
    print("Sent:", ok)

def cmd_send_image(args):
    img_id = upload_image(args.file)
    if not img_id:
        print("upload failed")
        sys.exit(2)
    ok = send_image_message(args.chat_id, img_id)
    print("Sent image:", ok, "image_id:", img_id)

def main():
    p = argparse.ArgumentParser(description="Avito Messenger CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("subscribe"); sp.add_argument("--url"); sp.set_defaults(func=cmd_subscribe)
    sp = sub.add_parser("unsubscribe"); sp.add_argument("--url"); sp.set_defaults(func=cmd_unsubscribe)
    sp = sub.add_parser("subs"); sp.set_defaults(func=cmd_subs)

    sp = sub.add_parser("chats"); sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--unread-only", action="store_true")
    sp.add_argument("--types", nargs="*", default=None, help="u2i u2u")
    sp.set_defaults(func=cmd_chats)

    sp = sub.add_parser("chat"); sp.add_argument("chat_id"); sp.set_defaults(func=cmd_chat)
    sp = sub.add_parser("msgs"); sp.add_argument("chat_id"); sp.add_argument("--limit", type=int, default=20); sp.add_argument("--offset", type=int, default=0); sp.set_defaults(func=cmd_msgs)
    sp = sub.add_parser("read"); sp.add_argument("chat_id"); sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("send-text"); sp.add_argument("chat_id"); sp.add_argument("text"); sp.set_defaults(func=cmd_send_text)
    sp = sub.add_parser("send-image"); sp.add_argument("chat_id"); sp.add_argument("file"); sp.set_defaults(func=cmd_send_image)

    args = p.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()

