"""
Управление списком Avito аккаунтов (multi-account) и их статусом.

Храним в data/avito_accounts.json список объектов:
[
  {
    "account_id": "123456",
    "name": "VisaWay Pro (опционально)",
    "paused": false,
    "mode": "full",
    "partial_percentage": 50,
    "created_at": "...",
    "updated_at": "..."
  }
]

Paused означает: бот НЕ отвечает за этот аккаунт, но продолжает слушать и учиться (chat_history → knowledge cards).

ВАЖНО ПО БЕЗОПАСНОСТИ:
- Секреты (client_id/client_secret) НЕ храним в JSON/репозитории.
- Credentials берём ТОЛЬКО из переменных окружения (.env) через `config.get_avito_credentials()`.
"""

from __future__ import annotations

import json
import os
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config import AVITO_ACCOUNTS_PATH, AVITO_ACCOUNT_ID, DATA_DIR, AVITO_CREDENTIALS_STORE_PATH

logger = logging.getLogger(__name__)

DEFAULT_MODE: str = "full"  # listening | partial | full
DEFAULT_PARTIAL_PERCENTAGE: int = 50

# legacy-поля, которые могли быть записаны раньше (до выноса секретов в .env)
_LEGACY_SECRET_FIELDS: tuple[str, ...] = ("client_id", "client_secret")


def _now_iso() -> str:
    return datetime.now().isoformat()


def _safe_load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _safe_save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_account_id(account_id: Any) -> Optional[str]:
    if account_id is None:
        return None
    s = str(account_id).strip()
    if not s:
        return None
    if not s.isdigit():
        return None
    return s


def ensure_accounts_store_initialized() -> None:
    """
    Гарантирует наличие файла avito_accounts.json.

    Важно (UX): дефолтный AVITO_ACCOUNT_ID из .env добавляем только при ПЕРВОМ создании файла.
    Если файл уже существует — НЕ навязываем дефолтный аккаунт, чтобы админ мог удалить все аккаунты
    и вести список полностью через Telegram.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.exists(AVITO_ACCOUNTS_PATH)
    data = _safe_load_json(AVITO_ACCOUNTS_PATH, [])
    if not isinstance(data, list):
        data = []

    changed = False
    default_id = normalize_account_id(AVITO_ACCOUNT_ID)
    if default_id:
        if not file_exists:
            # Первичная инициализация: можем добавить дефолтный аккаунт из .env
            data.append(
                {
                    "account_id": default_id,
                    "name": "default",
                    "paused": False,
                    "mode": DEFAULT_MODE,
                    "partial_percentage": DEFAULT_PARTIAL_PERCENTAGE,
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
            changed = True
        else:
            # Поддержка старого формата — добавим недостающие поля у уже существующей записи (если она есть)
            for x in data:
                if not isinstance(x, dict):
                    continue
                if normalize_account_id(x.get("account_id")) == default_id:
                    if "mode" not in x:
                        x["mode"] = DEFAULT_MODE
                        changed = True
                    if "partial_percentage" not in x:
                        x["partial_percentage"] = DEFAULT_PARTIAL_PERCENTAGE
                        changed = True
                    if "paused" not in x:
                        x["paused"] = False
                        changed = True
                    if "name" not in x:
                        x["name"] = "default"
                        changed = True
                    if changed:
                        x["updated_at"] = _now_iso()

    # Чистим legacy секреты из файла, если они там внезапно оказались
    for x in data:
        if isinstance(x, dict):
            for k in _LEGACY_SECRET_FIELDS:
                if k in x:
                    x.pop(k, None)
                    changed = True

    if not file_exists or changed:
        _safe_save_json(AVITO_ACCOUNTS_PATH, data)


def list_accounts() -> List[Dict[str, Any]]:
    ensure_accounts_store_initialized()
    data = _safe_load_json(AVITO_ACCOUNTS_PATH, [])
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and normalize_account_id(item.get("account_id")):
            # Удаляем legacy секреты, если они были записаны в старых версиях
            for k in _LEGACY_SECRET_FIELDS:
                if k in item:
                    item.pop(k, None)

            # Нормализуем недостающие поля для обратной совместимости
            item.setdefault("name", "")
            item.setdefault("paused", False)
            item.setdefault("mode", DEFAULT_MODE)
            item.setdefault("partial_percentage", DEFAULT_PARTIAL_PERCENTAGE)
            out.append(item)
    # стабильная сортировка: по account_id
    out.sort(key=lambda x: str(x.get("account_id")))
    return out


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    aid = normalize_account_id(account_id)
    if not aid:
        return None
    for a in list_accounts():
        if normalize_account_id(a.get("account_id")) == aid:
            return a
    return None


def upsert_account(
    account_id: str,
    *,
    name: Optional[str] = None,
    paused: Optional[bool] = None,
) -> Tuple[bool, str]:
    """
    Создаёт аккаунт или обновляет его поля (name/paused).
    """
    ensure_accounts_store_initialized()
    aid = normalize_account_id(account_id)
    if not aid:
        return False, "Некорректный account_id (ожидается числовой ID)."

    data = _safe_load_json(AVITO_ACCOUNTS_PATH, [])
    if not isinstance(data, list):
        data = []

    now = _now_iso()
    for item in data:
        if not isinstance(item, dict):
            continue
        if normalize_account_id(item.get("account_id")) == aid:
            if name is not None:
                item["name"] = str(name).strip()
            if paused is not None:
                item["paused"] = bool(paused)
            item.setdefault("mode", DEFAULT_MODE)
            item.setdefault("partial_percentage", DEFAULT_PARTIAL_PERCENTAGE)
            item["updated_at"] = now
            _safe_save_json(AVITO_ACCOUNTS_PATH, data)
            return True, "Аккаунт обновлён."

    data.append(
        {
            "account_id": aid,
            "name": (str(name).strip() if name else ""),
            "paused": bool(paused) if paused is not None else False,
            "mode": DEFAULT_MODE,
            "partial_percentage": DEFAULT_PARTIAL_PERCENTAGE,
            "created_at": now,
            "updated_at": now,
        }
    )
    _safe_save_json(AVITO_ACCOUNTS_PATH, data)
    return True, "Аккаунт добавлен."


def set_mode(account_id: str, mode: str, *, partial_percentage: Optional[int] = None) -> Tuple[bool, str]:
    """
    Устанавливает режим работы бота для конкретного Avito аккаунта.

    mode:
      - listening: только учится, не отвечает
      - partial: отвечает на часть сообщений (partial_percentage)
      - full: отвечает на все сообщения (если не waiting_manager/cooldown)
    """
    ensure_accounts_store_initialized()
    aid = normalize_account_id(account_id)
    if not aid:
        return False, "Некорректный account_id."

    mode = (mode or "").strip().lower()
    if mode not in ("listening", "partial", "full"):
        return False, "Некорректный режим. Доступно: listening/partial/full."

    data = _safe_load_json(AVITO_ACCOUNTS_PATH, [])
    if not isinstance(data, list):
        data = []

    now = _now_iso()
    for item in data:
        if not isinstance(item, dict):
            continue
        if normalize_account_id(item.get("account_id")) == aid:
            item["mode"] = mode
            if partial_percentage is not None:
                try:
                    p = int(partial_percentage)
                except Exception:
                    return False, "partial_percentage должен быть числом 0-100."
                p = max(0, min(100, p))
                item["partial_percentage"] = p
            else:
                item.setdefault("partial_percentage", DEFAULT_PARTIAL_PERCENTAGE)
            item.setdefault("paused", False)
            item.setdefault("name", "")
            item["updated_at"] = now
            _safe_save_json(AVITO_ACCOUNTS_PATH, data)
            return True, "Режим аккаунта обновлён."

    # Если аккаунта ещё нет — создаём (по умолчанию paused=True для безопасности)
    p = DEFAULT_PARTIAL_PERCENTAGE
    if partial_percentage is not None:
        try:
            p = int(partial_percentage)
        except Exception:
            return False, "partial_percentage должен быть числом 0-100."
        p = max(0, min(100, p))
    data.append(
        {
            "account_id": aid,
            "name": "",
            "paused": True,
            "mode": mode,
            "partial_percentage": p,
            "created_at": now,
            "updated_at": now,
        }
    )
    _safe_save_json(AVITO_ACCOUNTS_PATH, data)
    return True, "Аккаунт добавлен и режим установлен (paused=true)."


def set_paused(account_id: str, paused: bool) -> Tuple[bool, str]:
    return upsert_account(account_id, paused=paused)


def get_account_credentials(account_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает (client_id, client_secret) для account_id из .env (секреты не храним в JSON).
    """
    aid = normalize_account_id(account_id)
    if not aid:
        logger.debug("get_account_credentials: invalid account_id=%s", account_id)
        return None, None
    try:
        from config import get_avito_credentials

        cid, csec = get_avito_credentials(aid)
        if not cid or not csec:
            return None, None
        return cid, csec
    except Exception:
        return None, None


def set_account_credentials(account_id: str, client_id: str, client_secret: str) -> Tuple[bool, str]:
    """
    Устанавливает client_id/client_secret для аккаунта через Telegram.

    Безопасность:
    - Секреты НЕ пишутся в avito_accounts.json
    - Сохраняются в локальный файл `data/avito_credentials.json` (должен быть в .gitignore)
    - Работает без рестарта: `config.get_avito_credentials()` читает этот файл при каждом запросе
    """
    aid = normalize_account_id(account_id)
    if not aid:
        return False, "Некорректный account_id."
    cid = (str(client_id or "").strip())
    csec = (str(client_secret or "").strip())
    if not cid or not csec:
        return False, "client_id/client_secret не должны быть пустыми."
    try:
        store_path = str(AVITO_CREDENTIALS_STORE_PATH or "").strip() or os.path.join(DATA_DIR, "avito_credentials.json")
        os.makedirs(os.path.dirname(store_path) or ".", exist_ok=True)
        data = _safe_load_json(store_path, {})
        if not isinstance(data, dict):
            data = {}
        data[aid] = {
            "client_id": cid,
            "client_secret": csec,
            "updated_at": _now_iso(),
        }
        _safe_save_json(store_path, data)
        return True, "Credentials сохранены локально (data/avito_credentials.json)."
    except Exception as e:
        logger.exception("Failed to save avito credentials for account_id=%s: %s", aid, e)
        return False, "Не удалось сохранить credentials (ошибка записи файла)."


def delete_account_credentials(account_id: str) -> Tuple[bool, str]:
    """
    Удаляет credentials для аккаунта из локального хранилища.
    """
    aid = normalize_account_id(account_id)
    if not aid:
        return False, "Некорректный account_id."
    try:
        store_path = str(AVITO_CREDENTIALS_STORE_PATH or "").strip() or os.path.join(DATA_DIR, "avito_credentials.json")
        data = _safe_load_json(store_path, {})
        if not isinstance(data, dict):
            return False, "Хранилище credentials повреждено."
        if aid not in data:
            return False, "Для аккаунта credentials не заданы."
        data.pop(aid, None)
        _safe_save_json(store_path, data)
        return True, "Credentials удалены."
    except Exception as e:
        logger.exception("Failed to delete avito credentials for account_id=%s: %s", aid, e)
        return False, "Не удалось удалить credentials (ошибка записи файла)."


def delete_account(account_id: str) -> Tuple[bool, str]:
    ensure_accounts_store_initialized()
    aid = normalize_account_id(account_id)
    if not aid:
        return False, "Некорректный account_id."

    data = _safe_load_json(AVITO_ACCOUNTS_PATH, [])
    if not isinstance(data, list):
        data = []

    before = len(data)
    data = [x for x in data if not (isinstance(x, dict) and normalize_account_id(x.get("account_id")) == aid)]
    if len(data) == before:
        return False, "Аккаунт не найден."

    _safe_save_json(AVITO_ACCOUNTS_PATH, data)
    return True, "Аккаунт удалён."


def is_account_paused(account_id: Optional[str]) -> bool:
    aid = normalize_account_id(account_id)
    if not aid:
        return False
    acc = get_account(aid)
    if not acc:
        return False
    return bool(acc.get("paused", False))


def register_seen_account(account_id: Optional[str], *, name: Optional[str] = None) -> None:
    """
    Если webhook/чат подсказал account_id, регистрируем его в списке аккаунтов.
    По умолчанию добавляем как paused=True (безопасно), чтобы админ включил вручную.
    """
    aid = normalize_account_id(account_id)
    if not aid:
        return
    existing = get_account(aid)
    if existing:
        # обновим name, если появился
        if name and not (existing.get("name") or "").strip():
            upsert_account(aid, name=name)
        return
    upsert_account(aid, name=name, paused=True)
    logger.info("Discovered new Avito account_id=%s → added as paused", aid)


