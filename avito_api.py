"""
API клиент для работы с Avito Messenger API.

Модуль предоставляет функции для взаимодействия с Avito API:
- Получение и обновление токенов доступа
- Управление webhook подписками
- Отправка сообщений в чаты
- Работа с чатами и сообщениями
"""
import os
import requests
import time
import logging
from typing import Optional, Dict, Any, List, Tuple
from config import AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_ACCOUNT_ID

logger = logging.getLogger(__name__)

# Общая HTTP-сессия + ретраи (устойчивость к 429/5xx/флапающим сетям)
#
# ВАЖНО:
# - Таймауты задаются на уровне вызова (timeout=...)
# - Ретраи здесь НЕ “бесконечные”: ограничены, с backoff
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    _RETRY = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    _HTTP = requests.Session()
    _HTTP.mount("https://", HTTPAdapter(max_retries=_RETRY))
    _HTTP.mount("http://", HTTPAdapter(max_retries=_RETRY))
except Exception:  # pragma: no cover
    _HTTP = requests

# API endpoints
TOKEN_URL: str = "https://api.avito.ru/token"
API_BASE_V1: str = "https://api.avito.ru/messenger/v1/accounts"
API_BASE_V2: str = "https://api.avito.ru/messenger/v2/accounts"
API_BASE_V3: str = "https://api.avito.ru/messenger/v3/accounts"
WEBHOOK_V3: str = "https://api.avito.ru/messenger/v3/webhook"

# Константы для retry и timeout
TOKEN_REFRESH_TIMEOUT: int = 15
REQUEST_TIMEOUT: int = 20
IMAGE_UPLOAD_TIMEOUT: int = 60
WEBHOOK_TIMEOUT: int = 10

# Константы для валидации
MAX_ACCOUNT_ID: int = 2**63 - 1  # int64 max
MIN_CHAT_ID_LENGTH: int = 1
MIN_TEXT_LENGTH: int = 1

# Кэш токенов: (client_id, client_secret) -> {"access_token": str, "expires_at": float}
_token_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}


def _resolve_creds(client_id: Optional[str], client_secret: Optional[str]) -> Tuple[str, str]:
    """
    Разрешает credentials: сначала использует переданные параметры, затем fallback на глобальные.
    """
    cid = (str(client_id).strip() if client_id is not None else (str(AVITO_CLIENT_ID).strip() if AVITO_CLIENT_ID else ""))
    csec = (str(client_secret).strip() if client_secret is not None else (str(AVITO_CLIENT_SECRET).strip() if AVITO_CLIENT_SECRET else ""))
    if not cid or not csec:
        raise RuntimeError("AVITO_CLIENT_ID / AVITO_CLIENT_SECRET not set (and not provided as parameters)")
    return cid, csec


def _refresh_token(*, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> None:
    """
    Обновляет токен доступа к Avito API.
    
    Raises:
        RuntimeError: Если не установлены AVITO_CLIENT_ID или AVITO_CLIENT_SECRET
        requests.RequestException: При ошибке запроса к API
    """
    cid, csec = _resolve_creds(client_id, client_secret)
    
    data = {
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": csec,
        "scope": "messenger:read messenger:write"
    }
    
    try:
        r = _HTTP.post(TOKEN_URL, data=data, timeout=TOKEN_REFRESH_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        access_token = j.get("access_token")
        expires_in = int(j.get("expires_in", 3600))
        expires_at = time.time() + expires_in - 60  # Обновляем за минуту до истечения
        _token_cache[(cid, csec)] = {"access_token": access_token, "expires_at": expires_at}
        logger.info("Avito token refreshed with scopes: messenger:read messenger:write (client_id=%s)", cid)
    except requests.exceptions.RequestException as e:
        logger.error("Failed to refresh Avito token: %s", e)
        raise


def _get_token(*, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> str:
    """
    Получает текущий токен доступа, обновляя его при необходимости.
    
    Returns:
        Токен доступа к Avito API
        
    Raises:
        RuntimeError: Если не удалось получить токен
    """
    cid, csec = _resolve_creds(client_id, client_secret)
    info = _token_cache.get((cid, csec))
    if not info or time.time() > float(info.get("expires_at") or 0.0):
        _refresh_token(client_id=cid, client_secret=csec)
        info = _token_cache.get((cid, csec))
    token = (info or {}).get("access_token") if isinstance(info, dict) else None
    if not token:
        raise RuntimeError("Failed to get Avito access token")
    return str(token)


def _headers(*, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> Dict[str, str]:
    """
    Формирует заголовки для запросов к Avito API.
    
    Returns:
        Словарь с заголовками, включая Authorization
    """
    return {"Authorization": f"Bearer {_get_token(client_id=client_id, client_secret=client_secret)}"}


def _validate_account_id(account_id: Optional[str]) -> bool:
    """
    Валидирует формат account_id.
    
    Args:
        account_id: ID аккаунта для проверки
        
    Returns:
        True если формат корректный, False иначе
    """
    if not account_id:
        return False
    
    try:
        account_id_int = int(account_id)
        if account_id_int <= 0 or account_id_int > MAX_ACCOUNT_ID:
            logger.error("Invalid account_id: %s (must be positive int64)", account_id)
            return False
        return True
    except ValueError:
        logger.error("Invalid account_id format: %s (must be numeric)", account_id)
        return False


# --------- Webhook v3 ----------
def subscribe_webhook(url_to_send: str, *, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> bool:
    """
    Подписывается на webhook уведомления от Avito.
    
    Args:
        url_to_send: URL для получения webhook уведомлений
        
    Returns:
        True если успешно, False при ошибке
    """
    if not url_to_send:
        logger.error("subscribe_webhook: empty URL")
        return False
    
    try:
        r = _HTTP.post(
            WEBHOOK_V3,
            headers={**_headers(client_id=client_id, client_secret=client_secret), "Content-Type": "application/json"},
            json={"url": url_to_send},
            timeout=WEBHOOK_TIMEOUT
        )
        if r.status_code in (200, 201):
            logger.info("Webhook subscribed successfully: %s", url_to_send)
            return True
        logger.error("subscribe_webhook failed: status_code=%s, response=%s", r.status_code, r.text)
        return False
    except Exception as e:
        logger.exception("subscribe_webhook exception: %s", e)
    return False


def get_subscriptions(*, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> Dict[str, Any]:
    """
    Получает список активных подписок на webhook.
    
    Returns:
        Словарь с информацией о подписках
        
    Raises:
        requests.RequestException: При ошибке запроса к API
    """
    url = "https://api.avito.ru/messenger/v1/subscriptions"
    r = _HTTP.post(url, headers=_headers(client_id=client_id, client_secret=client_secret), timeout=WEBHOOK_TIMEOUT)
    r.raise_for_status()
    return r.json()


def unsubscribe_webhook(url_to_stop: str, *, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> bool:
    """
    Отписывается от webhook уведомлений.
    
    Args:
        url_to_stop: URL для отписки
        
    Returns:
        True если успешно, False при ошибке
    """
    if not url_to_stop:
        logger.error("unsubscribe_webhook: empty URL")
        return False
    
    url = "https://api.avito.ru/messenger/v1/webhook/unsubscribe"
    try:
        r = _HTTP.post(
            url,
            headers={**_headers(client_id=client_id, client_secret=client_secret), "Content-Type": "application/json"},
            json={"url": url_to_stop},
            timeout=WEBHOOK_TIMEOUT
        )
        if r.status_code in (200, 204):
            logger.info("Webhook unsubscribed successfully: %s", url_to_stop)
            return True
        logger.error("unsubscribe_webhook failed: status_code=%s, response=%s", r.status_code, r.text)
        return False
    except Exception as e:
        logger.exception("unsubscribe_webhook exception: %s", e)
    return False


# --------- Messages ----------
def send_text_message(
    chat_id: str,
    text: str,
    *,
    account_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> bool:
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
    # Валидация входных данных
    logger.info("send_text_message: Starting validation - chat_id=%s, text_length=%d", chat_id, len(text) if text else 0)
    
    if not chat_id or len(chat_id) < MIN_CHAT_ID_LENGTH:
        logger.error("send_text_message: invalid chat_id (chat_id=%s, length=%d, min=%d)", 
                     chat_id, len(chat_id) if chat_id else 0, MIN_CHAT_ID_LENGTH)
        return False
    
    if not text or len(text.strip()) < MIN_TEXT_LENGTH:
        logger.error("send_text_message: invalid text (text_length=%d, min=%d)", 
                     len(text) if text else 0, MIN_TEXT_LENGTH)
        return False
    
    resolved_account_id = str(account_id).strip() if account_id is not None else (str(AVITO_ACCOUNT_ID).strip() if AVITO_ACCOUNT_ID else None)
    if not resolved_account_id:
        logger.error("send_text_message: account_id not provided and AVITO_ACCOUNT_ID not set")
        return False

    if not _validate_account_id(resolved_account_id):
        logger.error("send_text_message: invalid account_id format (account_id=%s)", resolved_account_id)
        return False
    
    # Проверяем credentials: сначала переданные параметры, затем глобальные
    resolved_client_id = (str(client_id).strip() if client_id else None) or (str(AVITO_CLIENT_ID).strip() if AVITO_CLIENT_ID else None)
    resolved_client_secret = (str(client_secret).strip() if client_secret else None) or (str(AVITO_CLIENT_SECRET).strip() if AVITO_CLIENT_SECRET else None)
    
    if not resolved_client_id or not resolved_client_secret:
        logger.error("send_text_message: client_id or client_secret not set")
        logger.error("   Provided client_id=%s, client_secret=%s", "set" if client_id else "not set", "set" if client_secret else "not set")
        logger.error("   Global AVITO_CLIENT_ID=%s, AVITO_CLIENT_SECRET=%s", 
                     "set" if AVITO_CLIENT_ID else "not set", "set" if AVITO_CLIENT_SECRET else "not set")
        return False
    
    logger.info("send_text_message: Validation passed - proceeding to send (using credentials for account_id=%s)", resolved_account_id)
    
    # Проверяем существование чата перед отправкой (опционально, для диагностики)
    try:
        chat_info = get_chat(chat_id, account_id=resolved_account_id, client_id=resolved_client_id, client_secret=resolved_client_secret)
        if not chat_info:
            logger.warning("Chat %s not found or inaccessible before sending message", chat_id)
            logger.warning("Attempting to send anyway, but 404 is likely")
    except Exception as e:
        logger.warning("Could not verify chat existence before sending: %s", e)
        # Продолжаем попытку отправки, даже если проверка не удалась
    
    # Формируем URL согласно документации
    url = f"{API_BASE_V1}/{resolved_account_id}/chats/{chat_id}/messages"
    
    # Формат payload согласно документации Avito API
    payload = {
        "message": {
            "text": text
        },
        "type": "text"
    }
    
    try:
        headers = {**_headers(client_id=resolved_client_id, client_secret=resolved_client_secret), "Content-Type": "application/json"}
        logger.info(
            "Sending message to Avito: account_id=%s, chat_id=%s, text_length=%d",
            resolved_account_id, chat_id, len(text)
        )
        logger.debug("Request URL: %s", url)
        logger.debug("Request payload: %s", payload)
        
        r = _HTTP.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        
        logger.info("Avito API response: status_code=%s, chat_id=%s", r.status_code, chat_id)
        
        if r.status_code in (200, 201):
            logger.info("Message sent successfully to Avito chat_id=%s", chat_id)
            try:
                response_data = r.json()
                logger.debug("Response data: %s", response_data)
            except ValueError:
                logger.debug("Response is not JSON")
            return True
        else:
            # Детальное логирование ошибки
            response_text = r.text[:2000] if r.text else "(пустой ответ)"
            error_details = {
                "status_code": r.status_code,
                "response_text": response_text,
                "chat_id": chat_id,
                "account_id": resolved_account_id,
                "url": url
            }
            logger.error("❌ ОШИБКА ОТПРАВКИ СООБЩЕНИЯ В AVITO")
            logger.error("Статус код: %s", r.status_code)
            logger.error("Chat ID: %s", chat_id)
            logger.error("Account ID: %s", resolved_account_id)
            logger.error("Длина текста: %d символов", len(text))
            logger.error("Ответ от API: %s", response_text)
            
            # Анализ ошибки из JSON ответа
            response_text_for_log = r.text[:2000] if r.text else "(пустой ответ)"
            _log_api_error(r.status_code, response_text_for_log, error_details)
            
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error("❌ ОШИБКА СЕТЕВОГО ЗАПРОСА ПРИ ОТПРАВКЕ СООБЩЕНИЯ")
        logger.error("Тип ошибки: %s", type(e).__name__)
        logger.error("Сообщение: %s", str(e))
        logger.error("Chat ID: %s", chat_id)
        logger.error("Account ID: %s", resolved_account_id)
        logger.exception("Полная информация об ошибке:")
        return False
    except Exception as e:
        logger.error("❌ НЕОЖИДАННАЯ ОШИБКА ПРИ ОТПРАВКЕ СООБЩЕНИЯ")
        logger.error("Тип ошибки: %s", type(e).__name__)
        logger.error("Сообщение: %s", str(e))
        logger.error("Chat ID: %s", chat_id)
        logger.error("Account ID: %s", resolved_account_id)
        logger.exception("Полная информация об ошибке:")
        return False


def _log_api_error(status_code: int, response_text: str, error_details: Dict[str, Any]) -> None:
    """
    Логирует детали ошибки API с понятными инструкциями.
    
    Args:
        status_code: HTTP статус код
        response_text: Текст ответа от API
        error_details: Дополнительные детали ошибки
    """
    try:
        if response_text and response_text != "(пустой ответ)":
            import json
            try:
                error_json = json.loads(response_text)
                logger.error("📋 JSON ответ от API: %s", json.dumps(error_json, ensure_ascii=False, indent=2))
                
                if "error" in error_json:
                    error_info = error_json.get("error")
                    logger.error("🔴 Информация об ошибке: %s", error_info)
                    
                    # Выводим все поля ошибки
                    if isinstance(error_info, dict):
                        for key, value in error_info.items():
                            logger.error("   %s: %s", key, value)
                
                # Специальная обработка для разных статус кодов
                if status_code == 403:
                    _log_403_error(error_info if "error" in error_json else None)
                
                if status_code == 400:
                    _log_400_error(error_info if "error" in error_json else None, error_details)
                
                if status_code == 404:
                    _log_404_error(error_info if "error" in error_json else None, error_details)
                
                if "message" in error_json:
                    logger.error("💬 Сообщение об ошибке от API: %s", error_json.get("message"))
            except json.JSONDecodeError:
                logger.error("⚠️ Ответ от API не является валидным JSON")
                logger.error("📄 Сырой ответ: %s", response_text[:1000])
        else:
            logger.error("⚠️ Пустой ответ от API")
            if status_code == 400:
                logger.error("❌ 400 Bad Request с пустым ответом - возможно проблема с форматом запроса")
                logger.error("   Проверьте формат данных в запросе")
            elif status_code == 404:
                _log_404_error(None, error_details)
    except Exception as e:
        logger.error("❌ Ошибка при обработке ответа от API: %s", e)
        logger.error("📄 Сырой ответ: %s", response_text[:2000] if response_text else "(нет ответа)")


def _log_403_error(error_info: Any) -> None:
    """Логирует детали ошибки 403 Permission Denied."""
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


def _log_400_error(error_info: Any, error_details: Dict[str, Any]) -> None:
    """Логирует детали ошибки 400 Bad Request."""
    logger.error("="*60)
    logger.error("❌ ОШИБКА 400: Bad Request")
    logger.error("="*60)
    logger.error("Возможные причины:")
    logger.error("1. Неправильный формат account_id")
    logger.error("   → account_id должен быть числовым ID (например: 25658340)")
    logger.error("   → Текущий account_id: %s (проверьте формат)", AVITO_ACCOUNT_ID)
    logger.error("2. Неправильный формат chat_id")
    logger.error("   → chat_id должен быть строкой (например: u2i-TLpmWPeZclN5LJJkjXcOuw)")
    logger.error("   → Текущий chat_id: %s", error_details.get("chat_id"))
    logger.error("3. Неправильный формат payload")
    logger.error("   → Проверьте, что payload соответствует формату API Avito")
    logger.error("4. URL содержит недопустимые символы")
    logger.error("   → URL: %s", error_details.get("url"))
    logger.error("="*60)
    logger.error("Проверьте формат account_id - он должен быть числовым значением")
    logger.error("="*60)


def _log_404_error(error_info: Any, error_details: Dict[str, Any]) -> None:
    """Логирует детали ошибки 404 Not Found."""
    logger.error("="*60)
    logger.error("❌ ОШИБКА 404: Not Found")
    logger.error("="*60)
    logger.error("Возможные причины:")
    logger.error("1. Чат не существует или был удален")
    logger.error("   → chat_id: %s", error_details.get("chat_id"))
    logger.error("   → Проверьте, что чат еще существует в Avito")
    logger.error("2. Неправильный chat_id")
    logger.error("   → chat_id должен быть в формате (например: u2i-TLpmWPeZclN5LJJkjXcOuw)")
    logger.error("   → Текущий chat_id: %s", error_details.get("chat_id"))
    logger.error("3. Чат принадлежит другому account_id")
    logger.error("   → Текущий account_id: %s", AVITO_ACCOUNT_ID)
    logger.error("   → Проверьте, что чат принадлежит этому аккаунту")
    logger.error("4. Неправильный URL или формат запроса")
    logger.error("   → URL: %s", error_details.get("url"))
    logger.error("="*60)
    logger.error("Рекомендации:")
    logger.error("1. Проверьте, что чат существует в Avito")
    logger.error("2. Убедитесь, что используете правильный account_id")
    logger.error("3. Попробуйте получить информацию о чате через get_chat() перед отправкой")
    logger.error("="*60)


# Alias for backward compatibility
send_message = send_text_message


def upload_image(
    filepath: str,
    *,
    account_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> Optional[str]:
    """
    Загружает изображение в Avito и возвращает image_id.
    
    Args:
        filepath: Путь к файлу изображения
        
    Returns:
        image_id при успехе, None при ошибке
    """
    if not filepath or not os.path.exists(filepath):
        logger.error("upload_image: invalid filepath: %s", filepath)
        return None
    
    resolved_account_id = str(account_id).strip() if account_id is not None else (str(AVITO_ACCOUNT_ID).strip() if AVITO_ACCOUNT_ID else None)
    if not resolved_account_id or not _validate_account_id(resolved_account_id):
        logger.error("upload_image: invalid account_id")
        return None
    
    url = f"{API_BASE_V1}/{resolved_account_id}/uploadImages"
    
    try:
        with open(filepath, "rb") as f:
            files = {"uploadfile[]": f}
            r = _HTTP.post(url, headers=_headers(client_id=client_id, client_secret=client_secret), files=files, timeout=IMAGE_UPLOAD_TIMEOUT)
        
        if r.status_code in (200, 201):
            data = r.json()
            image_id = next(iter(data.keys()), None)
            if image_id:
                logger.info("Image uploaded successfully: image_id=%s", image_id)
            return image_id
        logger.error("upload_image failed: status_code=%s, response=%s", r.status_code, r.text)
        return None
    except Exception as e:
        logger.exception("upload_image exception: %s", e)
    return None


def send_image_message(
    chat_id: str,
    image_id: str,
    *,
    account_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> bool:
    """
    Отправляет сообщение с изображением в Avito чат.
    
    Args:
        chat_id: ID чата
        image_id: ID изображения, полученного после загрузки
        
    Returns:
        True если успешно, False при ошибке
    """
    if not chat_id or not image_id:
        logger.error("send_image_message: invalid chat_id or image_id")
        return False
    
    resolved_account_id = str(account_id).strip() if account_id is not None else (str(AVITO_ACCOUNT_ID).strip() if AVITO_ACCOUNT_ID else None)
    if not resolved_account_id or not _validate_account_id(resolved_account_id):
        logger.error("send_image_message: invalid account_id")
        return False
    
    url = f"{API_BASE_V1}/{resolved_account_id}/chats/{chat_id}/messages/image"
    
    try:
        r = _HTTP.post(
            url,
            headers={**_headers(client_id=client_id, client_secret=client_secret), "Content-Type": "application/json"},
            json={"image_id": image_id},
            timeout=REQUEST_TIMEOUT
        )
        if r.status_code in (200, 201):
            logger.info("Image message sent successfully to chat_id=%s", chat_id)
            return True
        logger.error("send_image_message failed: status_code=%s, response=%s", r.status_code, r.text)
        return False
    except Exception as e:
        logger.exception("send_image_message exception: %s", e)
        return False


def delete_message(
    chat_id: str,
    message_id: str,
    *,
    account_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> bool:
    """
    Удаляет сообщение в Avito чате.
    
    Args:
        chat_id: ID чата
        message_id: ID сообщения для удаления
        
    Returns:
        True если успешно, False при ошибке
    """
    if not chat_id or not message_id:
        logger.error("delete_message: invalid chat_id or message_id")
        return False
    
    resolved_account_id = str(account_id).strip() if account_id is not None else (str(AVITO_ACCOUNT_ID).strip() if AVITO_ACCOUNT_ID else None)
    if not resolved_account_id or not _validate_account_id(resolved_account_id):
        logger.error("delete_message: invalid account_id")
        return False
    
    url = f"{API_BASE_V1}/{resolved_account_id}/chats/{chat_id}/messages/{message_id}"
    
    try:
        r = _HTTP.post(url, headers=_headers(client_id=client_id, client_secret=client_secret), timeout=REQUEST_TIMEOUT)
        if r.status_code in (200, 204):
            logger.info("Message deleted successfully: chat_id=%s, message_id=%s", chat_id, message_id)
            return True
        logger.error("delete_message failed: status_code=%s, response=%s", r.status_code, r.text)
        return False
    except Exception as e:
        logger.exception("delete_message exception: %s", e)
        return False


def mark_chat_read(chat_id: str, *, account_id: Optional[str] = None, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> bool:
    """
    Отмечает чат как прочитанный.
    
    Args:
        chat_id: ID чата
        
    Returns:
        True если успешно, False при ошибке
    """
    if not chat_id:
        logger.error("mark_chat_read: invalid chat_id")
        return False
    
    resolved_account_id = str(account_id).strip() if account_id is not None else (str(AVITO_ACCOUNT_ID).strip() if AVITO_ACCOUNT_ID else None)
    if not resolved_account_id or not _validate_account_id(resolved_account_id):
        logger.error("mark_chat_read: invalid account_id")
        return False
    
    url = f"{API_BASE_V1}/{resolved_account_id}/chats/{chat_id}/read"
    
    try:
        r = _HTTP.post(url, headers=_headers(client_id=client_id, client_secret=client_secret), timeout=WEBHOOK_TIMEOUT)
        if r.status_code in (200, 204):
            logger.info("Chat marked as read: chat_id=%s", chat_id)
            return True
        logger.error("mark_chat_read failed: status_code=%s, response=%s", r.status_code, r.text)
        return False
    except Exception as e:
        logger.exception("mark_chat_read exception: %s", e)
        return False


# --------- Chats / Messages (read) ----------
def list_chats(
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    chat_types: Optional[List[str]] = None,
    *,
    account_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Получает список чатов.
    
    Args:
        limit: Количество чатов для запроса (1-100)
        offset: Сдвиг для пагинации (0-1000)
        unread_only: Возвращать только непрочитанные чаты
        chat_types: Типы чатов для фильтрации (u2i, u2u)
        
    Returns:
        Словарь с информацией о чатах или None при ошибке
        
    Raises:
        requests.RequestException: При ошибке запроса к API
    """
    resolved_account_id = str(account_id).strip() if account_id is not None else (str(AVITO_ACCOUNT_ID).strip() if AVITO_ACCOUNT_ID else None)
    if not resolved_account_id or not _validate_account_id(resolved_account_id):
        logger.error("list_chats: invalid account_id")
        return None
    
    url = f"{API_BASE_V2}/{resolved_account_id}/chats"
    params = {
        "limit": max(1, min(limit, 100)),
        "offset": max(0, min(offset, 1000)),
        "unread_only": str(bool(unread_only)).lower(),
    }
    if chat_types:
        params["chat_types"] = ",".join(chat_types)
    
    try:
        r = _HTTP.get(url, headers=_headers(client_id=client_id, client_secret=client_secret), params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response and e.response.status_code == 403:
            logger.error("list_chats: 403 Permission Denied - проверьте account_id и credentials")
        raise
    except Exception as e:
        logger.exception("list_chats error: %s", e)
        raise


def get_chat(chat_id: str, *, account_id: Optional[str] = None, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Получает информацию о чате.
    
    Args:
        chat_id: ID чата
        
    Returns:
        Словарь с информацией о чате или None при ошибке
        
    Raises:
        requests.RequestException: При ошибке запроса к API
    """
    if not chat_id:
        logger.error("get_chat: invalid chat_id")
        return None
    
    resolved_account_id = str(account_id).strip() if account_id is not None else (str(AVITO_ACCOUNT_ID).strip() if AVITO_ACCOUNT_ID else None)
    if not resolved_account_id or not _validate_account_id(resolved_account_id):
        logger.error("get_chat: invalid account_id")
        return None
    
    url = f"{API_BASE_V2}/{resolved_account_id}/chats/{chat_id}"
    
    try:
        r = _HTTP.get(url, headers=_headers(client_id=client_id, client_secret=client_secret), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response and e.response.status_code == 403:
            logger.error(
                "get_chat: 403 Permission Denied для chat_id=%s, account_id=%s",
                chat_id, resolved_account_id
            )
            logger.error("  Возможно, используется неправильный account_id или credentials")
        raise
    except Exception as e:
        logger.exception("get_chat error: %s", e)
        raise


def list_messages_v3(
    chat_id: str,
    limit: int = 50,
    offset: int = 0,
    *,
    account_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Получает список сообщений из чата (v3 API).
    
    Args:
        chat_id: ID чата
        limit: Количество сообщений (1-100)
        offset: Сдвиг для пагинации (0-1000)
        
    Returns:
        Список сообщений
        
    Raises:
        requests.RequestException: При ошибке запроса к API
    """
    if not chat_id:
        logger.error("list_messages_v3: invalid chat_id")
        return []
    
    resolved_account_id = str(account_id).strip() if account_id is not None else (str(AVITO_ACCOUNT_ID).strip() if AVITO_ACCOUNT_ID else None)
    if not resolved_account_id or not _validate_account_id(resolved_account_id):
        logger.error("list_messages_v3: invalid account_id")
        return []
    
    url = f"{API_BASE_V3}/{resolved_account_id}/chats/{chat_id}/messages/"
    params = {
        "limit": max(1, min(limit, 100)),
        "offset": max(0, min(offset, 1000))
    }
    
    try:
        r = _HTTP.get(url, headers=_headers(client_id=client_id, client_secret=client_secret), params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        return j if isinstance(j, list) else []
    except Exception as e:
        logger.exception("list_messages_v3 error: %s", e)
        raise
