# avito_api.py
import requests
import time
import logging
from typing import Optional, Dict, Any, List
from config import AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_ACCOUNT_ID

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.avito.ru/token"
API_BASE_V1 = "https://api.avito.ru/messenger/v1/accounts"
API_BASE_V2 = "https://api.avito.ru/messenger/v2/accounts"
API_BASE_V3 = "https://api.avito.ru/messenger/v3/accounts"
WEBHOOK_V3  = "https://api.avito.ru/messenger/v3/webhook"

_access_token = None
_expires_at = 0.0

def _refresh_token():
    global _access_token, _expires_at
    if not AVITO_CLIENT_ID or not AVITO_CLIENT_SECRET:
        raise RuntimeError("AVITO_CLIENT_ID / AVITO_CLIENT_SECRET not set")
    data = {
        "grant_type": "client_credentials",
        "client_id": AVITO_CLIENT_ID,
        "client_secret": AVITO_CLIENT_SECRET,
        # Указываем необходимые scopes для работы с мессенджером
        "scope": "messenger:read messenger:write"
    }
    r = requests.post(TOKEN_URL, data=data, timeout=15)
    r.raise_for_status()
    j = r.json()
    _access_token = j.get("access_token")
    _expires_at = time.time() + int(j.get("expires_in", 3600)) - 60
    logger.info("Avito token refreshed with scopes: messenger:read messenger:write")

def _get_token():
    global _access_token, _expires_at
    if not _access_token or time.time() > _expires_at:
        _refresh_token()
    return _access_token

def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_get_token()}"}

# --------- Webhook v3 ----------
def subscribe_webhook(url_to_send: str) -> bool:
    r = requests.post(WEBHOOK_V3, headers={**_headers(), "Content-Type":"application/json"},
                      json={"url": url_to_send}, timeout=10)
    if r.status_code in (200, 201):
        return True
    logger.error("subscribe_webhook failed %s %s", r.status_code, r.text)
    return False

def get_subscriptions() -> Dict[str, Any]:
    url = "https://api.avito.ru/messenger/v1/subscriptions"
    r = requests.post(url, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()

def unsubscribe_webhook(url_to_stop: str) -> bool:
    url = "https://api.avito.ru/messenger/v1/webhook/unsubscribe"
    r = requests.post(url, headers={**_headers(), "Content-Type":"application/json"},
                      json={"url": url_to_stop}, timeout=10)
    if r.status_code in (200, 204):
        return True
    logger.error("unsubscribe_webhook failed %s %s", r.status_code, r.text)
    return False

# --------- Messages ----------
def send_text_message(chat_id: str, text: str) -> bool:
    """
    Отправляет текстовое сообщение в Avito чат.
    
    Согласно документации Avito API:
    POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/messages
    
    Args:
        chat_id: ID чата (например, "u2i-TLpmWPeZclN5LJJkjXcOuw")
        text: Текст сообщения
    
    Returns:
        True если успешно, False при ошибке
    """
    if not chat_id or not text:
        logger.error("send_text_message: empty chat_id or text (chat_id=%s, text_length=%d)", 
                     chat_id, len(text) if text else 0)
        return False
    
    if not AVITO_ACCOUNT_ID:
        logger.error("send_text_message: AVITO_ACCOUNT_ID not set! Please set AVITO_ACCOUNT_ID in .env file")
        logger.error("  This is the user_id (account ID) of your Avito company account")
        logger.error("  Without this, messages cannot be sent to Avito")
        return False
    
    # Проверяем формат account_id - должен быть числовым ID
    # Согласно документации Avito: user_id - required - integer <int64>
    try:
        # Попробуем преобразовать в число для проверки формата
        account_id_int = int(AVITO_ACCOUNT_ID)
        if account_id_int <= 0:
            logger.error("send_text_message: AVITO_ACCOUNT_ID must be a positive integer, got: %s", AVITO_ACCOUNT_ID)
            return False
        logger.debug("Account ID format check passed: %s (integer)", account_id_int)
    except ValueError:
        # Если не число, возможно это строка-хеш, что неправильно
        logger.error("="*60)
        logger.error("❌ ОШИБКА: Неправильный формат AVITO_ACCOUNT_ID")
        logger.error("="*60)
        logger.error("AVITO_ACCOUNT_ID должен быть числовым ID (например: 25658340)")
        logger.error("Текущий AVITO_ACCOUNT_ID: %s (похоже на хеш/строку)", AVITO_ACCOUNT_ID)
        logger.error("="*60)
        logger.error("Согласно документации Avito API:")
        logger.error("  user_id - required - integer <int64> - Идентификатор пользователя (клиента)")
        logger.error("="*60)
        logger.error("Проверьте .env файл и убедитесь, что AVITO_ACCOUNT_ID - это числовой ID")
        logger.error("Например: AVITO_ACCOUNT_ID=25658340")
        logger.error("="*60)
        return False
    
    # Проверяем другие необходимые переменные
    if not AVITO_CLIENT_ID or not AVITO_CLIENT_SECRET:
        logger.error("send_text_message: AVITO_CLIENT_ID or AVITO_CLIENT_SECRET not set")
        return False
    
    # Формируем URL согласно документации: /messenger/v1/accounts/{user_id}/chats/{chat_id}/messages
    url = f"{API_BASE_V1}/{AVITO_ACCOUNT_ID}/chats/{chat_id}/messages"
    
    # Формат payload согласно документации Avito API
    payload = {
        "message": {
            "text": text
        },
        "type": "text"
    }
    
    try:
        headers = {**_headers(), "Content-Type": "application/json"}
        logger.info("Sending message to Avito: account_id=%s, chat_id=%s, text_length=%d, url=%s", 
                   AVITO_ACCOUNT_ID, chat_id, len(text), url)
        logger.debug("Request payload: %s", payload)
        logger.debug("Request headers (без токена): %s", {k: v for k, v in headers.items() if k != "Authorization"})
        
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        
        # Логируем детали ответа
        logger.info("Avito API response: status_code=%s, chat_id=%s", r.status_code, chat_id)
        
        if r.status_code in (200, 201):
            logger.info("Message sent successfully to Avito chat_id=%s", chat_id)
            try:
                response_data = r.json()
                logger.debug("Response data: %s", response_data)
            except:
                pass
            return True
        else:
            # Детальное логирование ошибки
            response_text = r.text[:1000] if r.text else "(empty response)"
            error_details = {
                "status_code": r.status_code,
                "response_text": response_text,
                "response_headers": dict(r.headers),
                "chat_id": chat_id,
                "account_id": AVITO_ACCOUNT_ID,
                "url": url,
                "payload": payload
            }
            logger.error("send_text_message failed: %s", error_details)
            
            # Попробуем понять ошибку из JSON ответа
            try:
                if r.text:
                    error_json = r.json()
                    logger.error("Error JSON: %s", error_json)
                    if "error" in error_json:
                        error_info = error_json.get("error")
                        logger.error("API Error: %s", error_info)
                        
                        # Специальная обработка для 403 ошибки
                        if r.status_code == 403:
                            error_msg = error_info.get("message", "") if isinstance(error_info, dict) else str(error_info)
                            logger.error("="*60)
                            logger.error("❌ ОШИБКА 403: Permission Denied")
                            logger.error("="*60)
                            logger.error("Возможные причины:")
                            logger.error("1. Используется account_id сотрудника, а не основного аккаунта компании")
                            logger.error("   → Нужно использовать account_id основного аккаунта компании")
                            logger.error("2. Используются credentials сотрудника вместо credentials компании")
                            logger.error("   → Нужно использовать AVITO_CLIENT_ID и AVITO_CLIENT_SECRET компании")
                            logger.error("3. У приложения нет прав messenger:write")
                            logger.error("   → Проверьте настройки приложения в личном кабинете Avito")
                            logger.error("4. Чатом управляет другой аккаунт/объявление")
                            logger.error("   → Проверьте, что вы используете правильный account_id")
                            logger.error("="*60)
                            logger.error("Текущий account_id: %s", AVITO_ACCOUNT_ID)
                            logger.error("Проверьте, что это ID основного аккаунта компании, а не сотрудника")
                            logger.error("="*60)
                        
                        # Специальная обработка для 400 ошибки
                        if r.status_code == 400:
                            error_msg = error_info.get("message", "") if isinstance(error_info, dict) else str(error_info)
                            logger.error("="*60)
                            logger.error("❌ ОШИБКА 400: Bad Request")
                            logger.error("="*60)
                            logger.error("Возможные причины:")
                            logger.error("1. Неправильный формат account_id")
                            logger.error("   → account_id должен быть числовым ID (например: 25658340)")
                            logger.error("   → Текущий account_id: %s (проверьте формат)", AVITO_ACCOUNT_ID)
                            logger.error("2. Неправильный формат chat_id")
                            logger.error("   → chat_id должен быть строкой (например: u2i-TLpmWPeZclN5LJJkjXcOuw)")
                            logger.error("   → Текущий chat_id: %s", chat_id)
                            logger.error("3. Неправильный формат payload")
                            logger.error("   → Проверьте, что payload соответствует формату API Avito")
                            logger.error("   → Payload: %s", payload)
                            logger.error("4. URL содержит недопустимые символы")
                            logger.error("   → URL: %s", url)
                            logger.error("="*60)
                            logger.error("Проверьте формат account_id - он должен быть числовым значением")
                            logger.error("="*60)
                        
                        if "message" in error_json:
                            logger.error("API Error message: %s", error_json.get("message"))
                else:
                    logger.error("Empty response body from API")
                    if r.status_code == 400:
                        logger.error("400 Bad Request with empty response - возможно проблема с форматом запроса")
                        logger.error("Проверьте:")
                        logger.error("  1. Формат account_id (должен быть числом, не строкой)")
                        logger.error("  2. Формат chat_id")
                        logger.error("  3. Формат payload")
            except ValueError as e:
                logger.error("Could not parse error response as JSON. Error: %s", e)
                logger.error("Raw response: %s", r.text[:1000] if r.text else "(empty)")
                if r.status_code == 400:
                    logger.error("400 Bad Request - проверьте формат запроса")
            
            return False
            
    except requests.exceptions.RequestException as e:
        logger.exception("Request exception in send_text_message: %s, chat_id=%s, account_id=%s", 
                        e, chat_id, AVITO_ACCOUNT_ID)
        return False
    except Exception as e:
        logger.exception("Unexpected exception in send_text_message: %s, chat_id=%s, account_id=%s", 
                        e, chat_id, AVITO_ACCOUNT_ID)
        return False

# Alias for backward compatibility
send_message = send_text_message

def upload_image(filepath: str) -> Optional[str]:
    url = f"{API_BASE_V1}/{AVITO_ACCOUNT_ID}/uploadImages"
    with open(filepath, "rb") as f:
        files = {"uploadfile[]": f}
        r = requests.post(url, headers=_headers(), files=files, timeout=60)
    if r.status_code in (200, 201):
        data = r.json()
        return next(iter(data.keys()), None)
    return None

def send_image_message(chat_id: str, image_id: str) -> bool:
    url = f"{API_BASE_V1}/{AVITO_ACCOUNT_ID}/chats/{chat_id}/messages/image"
    r = requests.post(url, headers={**_headers(), "Content-Type":"application/json"},
                      json={"image_id": image_id}, timeout=20)
    return r.status_code in (200, 201)

def delete_message(chat_id: str, message_id: str) -> bool:
    url = f"{API_BASE_V1}/{AVITO_ACCOUNT_ID}/chats/{chat_id}/messages/{message_id}"
    r = requests.post(url, headers=_headers(), timeout=15)
    return r.status_code in (200, 204)

def mark_chat_read(chat_id: str) -> bool:
    url = f"{API_BASE_V1}/{AVITO_ACCOUNT_ID}/chats/{chat_id}/read"
    r = requests.post(url, headers=_headers(), timeout=10)
    return r.status_code in (200, 204)

# --------- Chats / Messages (read) ----------
def list_chats(limit:int=50, offset:int=0, unread_only:bool=False, chat_types:list[str]|None=None):
    """Получает список чатов. Может помочь определить правильный account_id."""
    if not AVITO_ACCOUNT_ID:
        logger.error("list_chats: AVITO_ACCOUNT_ID not set")
        return None
    
    url = f"{API_BASE_V2}/{AVITO_ACCOUNT_ID}/chats"
    params = {
        "limit": max(1, min(limit, 100)),
        "offset": max(0, min(offset, 1000)),
        "unread_only": str(bool(unread_only)).lower(),
    }
    if chat_types:
        params["chat_types"] = ",".join(chat_types)
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            logger.error("list_chats: 403 Permission Denied - проверьте account_id и credentials")
        raise
    except Exception as e:
        logger.exception("list_chats error: %s", e)
        raise

def get_chat(chat_id:str):
    """Получает информацию о чате. Может помочь проверить права доступа."""
    if not AVITO_ACCOUNT_ID:
        logger.error("get_chat: AVITO_ACCOUNT_ID not set")
        return None
    
    url = f"{API_BASE_V2}/{AVITO_ACCOUNT_ID}/chats/{chat_id}"
    try:
        r = requests.get(url, headers=_headers(), timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            logger.error("get_chat: 403 Permission Denied для chat_id=%s, account_id=%s", 
                        chat_id, AVITO_ACCOUNT_ID)
            logger.error("  Возможно, используется неправильный account_id или credentials")
        raise
    except Exception as e:
        logger.exception("get_chat error: %s", e)
        raise

def list_messages_v3(chat_id:str, limit:int=50, offset:int=0) -> list:
    url = f"{API_BASE_V3}/{AVITO_ACCOUNT_ID}/chats/{chat_id}/messages/"
    params = {"limit": max(1,min(limit,100)), "offset": max(0,min(offset,1000))}
    r = requests.get(url, headers=_headers(), params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    return j if isinstance(j, list) else []
