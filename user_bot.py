"""
Модуль обработчиков команд и сообщений для Telegram бота.

Содержит обработчики для:
- Команд пользователей (/start)
- Административных команд (/uploadfaq, /setcontext, /subscribe, /unsubscribe)
- Обычных сообщений пользователей (генерация ответов через LLM)
- Обратной связи (👍 👎)
"""
import os
import random
import time
import json
import logging
import re
import httpx
from typing import Dict, Any, List, Optional
from aiogram import F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.chat_action import ChatActionSender
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import AsyncOpenAI
from bs4 import BeautifulSoup

from create_bot import bot
from config import (
    FAQ_PATH, STATIC_CONTEXT_PATH, CHAT_HISTORY_PATH,
    LLM_MODEL, TEMPERATURE, OPENAI_API_KEY, ADMINS, WEBHOOK_URL, DATA_DIR
)
from responder import generate_reply
from avito_api import subscribe_webhook, unsubscribe_webhook

# Константы
MAX_FAQ_CHUNK_SIZE: int = 6000
SYSTEM_MESSAGE_PREFIXES: List[str] = ["Системное:", "Сообщение отправлено"]
DIALOG_ID_PATTERN: re.Pattern = re.compile(r";([0-9]+:m:[^:]+):")
DIALOG_ID_CLEANUP_PATTERN: re.Pattern = re.compile(r"[a-z0-9]+;[0-9]+:m:[^:]+:[0-9]+$")
NAME_PATTERN: re.Pattern = re.compile(r"^([\wА-Яа-яёЁ]+):\s*(.+)")
HISTORY_PATTERN: re.Pattern = re.compile(r"ИСТОРИЯ(.+)", re.DOTALL)
SUBSCRIBE_PATTERN: re.Pattern = re.compile(r"^/subscribe\b")
UNSUBSCRIBE_PATTERN: re.Pattern = re.compile(r"^/unsubscribe\b")

# Инициализация
user_router = Router()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

# Инициализация OpenAI клиента
http_client = httpx.AsyncClient()
client = AsyncOpenAI(api_key=OPENAI_API_KEY, http_client=http_client)

# Инициализация директории данных
os.makedirs(DATA_DIR, exist_ok=True)

# Инициализация файлов
if not os.path.exists(FAQ_PATH):
    with open(FAQ_PATH, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)
if not os.path.exists(STATIC_CONTEXT_PATH):
    with open(STATIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
        f.write("")

# Временное хранилище для рейтинга (в production можно использовать Redis/БД)
TEMP_QA: Dict[str, Dict[str, str]] = {}


class AdminStates(StatesGroup):
    """Состояния FSM для административных команд."""
    waiting_for_faq_file = State()
    waiting_for_static_context = State()


def _check_admin(user_id: int) -> bool:
    """
    Проверяет, является ли пользователь администратором.
    
    Args:
        user_id: ID пользователя в Telegram
        
    Returns:
        True если пользователь администратор, False иначе
    """
    return user_id in ADMINS if ADMINS else False


# ----------------------------
# /start
# ----------------------------
@user_router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    Обрабатывает команду /start.
    
    Приветствует пользователя и объясняет возможности бота.
    
    Args:
        message: Сообщение с командой /start
    """
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        text = (
            "Привет! Я цифровой помощник компании VisaWay!"
        )
        await message.answer(text)
        logger.info("/start вызван пользователем %d", message.from_user.id)


# ----------------------------
# /uploadfaq — админ
# ----------------------------
@user_router.message(F.text.startswith("/uploadfaq"))
async def cmd_upload_faq(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает команду /uploadfaq для загрузки FAQ файла.
    
    Args:
        message: Сообщение с командой
        state: FSM контекст для управления состоянием
    """
    if not _check_admin(message.from_user.id):
        logger.warning("Неавторизованный пользователь %d попытался загрузить FAQ", message.from_user.id)
        await message.answer("⛔️ У вас нет прав для загрузки FAQ.")
        return
    
    await message.answer("Отправьте файл FAQ (txt, html или csv)")
    await state.set_state(AdminStates.waiting_for_faq_file)


# ----------------------------
# /setcontext — админ
# ----------------------------
@user_router.message(F.text.startswith("/setcontext"))
async def cmd_set_context(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает команду /setcontext для установки статического контекста.
    
    Args:
        message: Сообщение с командой
        state: FSM контекст для управления состоянием
    """
    if not _check_admin(message.from_user.id):
        logger.warning("Неавторизованный пользователь %d попытался установить контекст", message.from_user.id)
        await message.answer("⛔️ У вас нет прав для загрузки контекста.")
        return
    
    await message.answer("Отправьте новый текст статичного контекста (он перезапишет старый).")
    await state.set_state(AdminStates.waiting_for_static_context)


@user_router.message(AdminStates.waiting_for_static_context)
async def handle_static_context(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает текст статического контекста от администратора.
    
    Args:
        message: Сообщение с текстом контекста
        state: FSM контекст для управления состоянием
    """
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение с контекстом.")
        await state.clear()
        return
    
    try:
        new_context = message.text.strip()
        with open(STATIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
            f.write(new_context)
        logger.info("Статичный контекст успешно обновлен пользователем %d", message.from_user.id)
        await message.answer("Контекст обновлён.")
    except Exception as e:
        logger.exception("Ошибка при сохранении контекста: %s", e)
        await message.answer("Ошибка при сохранении контекста.")
    finally:
        await state.clear()


# ----------------------------
# /subscribe и /unsubscribe — админ
# ----------------------------
@user_router.message(F.text.regexp(r"^/subscribe\b"))
async def tg_subscribe(message: Message) -> None:
    """
    Обрабатывает команду /subscribe для подписки на webhook от Avito.
    
    Args:
        message: Сообщение с командой
    """
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    if not WEBHOOK_URL:
        await message.answer("❗️Не задан PUBLIC_BASE_URL в .env")
        return
    
    ok = subscribe_webhook(WEBHOOK_URL)
    await message.answer("✅ Вебхук зарегистрирован." if ok else "❌ Ошибка регистрации вебхука.")


@user_router.message(F.text.regexp(r"^/unsubscribe\b"))
async def tg_unsubscribe(message: Message) -> None:
    """
    Обрабатывает команду /unsubscribe для отписки от webhook от Avito.
    
    Args:
        message: Сообщение с командой
    """
    if not _check_admin(message.from_user.id):
        await message.answer("⛔️ Недостаточно прав.")
        return
    
    if not WEBHOOK_URL:
        await message.answer("❗️Не задан PUBLIC_BASE_URL в .env")
        return
    
    ok = unsubscribe_webhook(WEBHOOK_URL)
    await message.answer("✅ Вебхук отключён." if ok else "❌ Ошибка отключения вебхука.")


# ----------------------------
# Обработка FAQ файла
# ----------------------------
@user_router.message(AdminStates.waiting_for_faq_file, F.document)
async def handle_faq_file(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает загрузку FAQ файла от администратора.
    
    Парсит файл (txt, html, csv) и использует LLM для структурирования
    вопросов и ответов в JSON формат.
    
    Args:
        message: Сообщение с документом
        state: FSM контекст для управления состоянием
    """
    if not message.document:
        await message.answer("Пожалуйста, отправьте файл.")
        await state.clear()
        return
    
    document = message.document
    if not document.file_name:
        await message.answer("Файл должен иметь имя.")
        await state.clear()
        return
    
    file_path = os.path.join(DATA_DIR, f"faq_upload_{int(time.time())}_{document.file_name}")
    
    try:
        await bot.download(file=document.file_id, destination=file_path)
        logger.info("Админ %d загрузил файл FAQ: %s", message.from_user.id, file_path)
    except Exception as e:
        logger.exception("Ошибка при загрузке файла: %s", e)
        await message.answer("Ошибка при загрузке файла.")
        await state.clear()
        return
    
    # Чтение и парсинг файла
    try:
        if document.file_name.endswith(".html"):
            with open(file_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            soup = BeautifulSoup(html_content, "html.parser")
            new_content = soup.get_text(separator="\n").strip()
        else:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                new_content = f.read().strip()
    except Exception as e:
        logger.exception("Ошибка при чтении файла: %s", e)
        await message.answer("Ошибка при чтении файла FAQ.")
        await state.clear()
        # Очищаем временный файл
        try:
            os.remove(file_path)
        except Exception:
            pass
        return
    
    if not new_content:
        await message.answer("Файл пуст или не удалось извлечь текст.")
        await state.clear()
        return
    
    # Разделяем контент на части для обработки LLM
    chunks = [
        new_content[i:i + MAX_FAQ_CHUNK_SIZE]
        for i in range(0, len(new_content), MAX_FAQ_CHUNK_SIZE)
    ]
    logger.info("Файл разделен на %d частей", len(chunks))
    
    # Загружаем существующий FAQ
    try:
        with open(FAQ_PATH, "r", encoding="utf-8") as f:
            current_faq = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        current_faq = []
    
    all_new_faq: List[Dict[str, str]] = []
    
    # Обрабатываем каждую часть через LLM
    for idx, chunk in enumerate(chunks, start=1):
        prompt = f"""
Ты — эксперт по международным визам. 
Вот часть новой информации из FAQ (часть {idx} из {len(chunks)}):

{chunk}

Задача: структурировать вопросы и ответы в JSON массив вида:
[
  {{"question": "...", "answer": "..."}}
]

Не дублируй одинаковые вопросы. 
Не включай ничего, что не относится к визам.
Отвечай только JSON — без текста, без комментариев.
"""
        try:
            # Проверяем, поддерживает ли модель temperature
            # Для gpt-5-mini и некоторых других моделей temperature не поддерживается
            use_temperature = LLM_MODEL not in ["gpt-5-mini", "gpt-5"]
            
            if use_temperature:
                response = await client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=TEMPERATURE,
                )
            else:
                response = await client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                )
            llm_response = response.choices[0].message.content.strip()
            
            # Извлекаем JSON из ответа
            match = re.search(r"\[.*\]", llm_response, re.DOTALL)
            if match:
                chunk_faq = json.loads(match.group(0))
                chunk_faq = [
                    {
                        "question": i.get("question", "").strip(),
                        "answer": i.get("answer", "").strip()
                    }
                    for i in chunk_faq
                    if isinstance(i, dict) and i.get("question") and i.get("answer")
                ]
                all_new_faq.extend(chunk_faq)
                logger.info("Обработана часть %d/%d, получено %d записей.", idx, len(chunks), len(chunk_faq))
            else:
                logger.warning("Часть %d не вернула корректный JSON", idx)
        except Exception as e:
            logger.exception("Ошибка LLM при обработке части %d: %s", idx, e)
            continue
    
    # Убираем дубли
    questions_existing = {item.get("question", "") for item in current_faq if item.get("question")}
    combined_faq = current_faq + [
        item for item in all_new_faq
        if item.get("question") and item["question"] not in questions_existing
    ]
    
    # Сохраняем обновленный FAQ
    try:
        with open(FAQ_PATH, "w", encoding="utf-8") as f:
            json.dump(combined_faq, f, ensure_ascii=False, indent=2)
        
        logger.info("FAQ обновлен. Добавлено %d новых записей.", len(all_new_faq))
        await message.answer(f"FAQ обновлен. Добавлено {len(all_new_faq)} записей.")
    except Exception as e:
        logger.exception("Ошибка при сохранении FAQ: %s", e)
        await message.answer("Ошибка при сохранении FAQ.")
    finally:
        # Очищаем временный файл
        try:
            os.remove(file_path)
        except Exception:
            pass
        await state.clear()


# ----------------------------
# Ответ пользователю в ТГ (через единый responder)
# ----------------------------
@user_router.message(F.text)
async def handle_user_message(message: Message) -> None:
    """
    Обрабатывает обычные текстовые сообщения от пользователей.
    
    Генерирует ответ через LLM на основе FAQ, истории диалога и статического контекста.
    
    Args:
        message: Текстовое сообщение от пользователя
    """
    if not message.text:
        return
    
    logger.info(
        "handle_user_message called: user_id=%d, text_length=%d",
        message.from_user.id, len(message.text)
    )
    
    raw_text = message.text.strip()
    
    # Игнорируем системные сообщения
    if any(raw_text.startswith(prefix) for prefix in SYSTEM_MESSAGE_PREFIXES):
        logger.debug("Skipping system message")
        return
    
    # Извлекаем dialog_id (может быть в специальном формате)
    m = DIALOG_ID_PATTERN.search(raw_text)
    dialog_id = m.group(1) if m else f"tg_{message.from_user.id}"
    
    # Очищаем хвост id из текста
    clean_text = DIALOG_ID_CLEANUP_PATTERN.sub("", raw_text).strip()
    
    # Извлекаем имя и вопрос (если есть формат "Имя: вопрос")
    name_m = NAME_PATTERN.search(clean_text)
    if name_m:
        user_name = name_m.group(1)
        user_question = name_m.group(2).strip()
    else:
        user_name = None
        user_question = clean_text
    
    # Извлекаем вложенную историю (если есть)
    hist_m = HISTORY_PATTERN.search(clean_text)
    embedded_history = hist_m.group(1).strip() if hist_m else ""
    
    # Генерируем ответ через единый модуль
    logger.info(
        "Calling generate_reply for dialog_id=%s, user_question_length=%d",
        dialog_id, len(user_question)
    )
    
    try:
        answer, _meta = await generate_reply(
            dialog_id=dialog_id,
            incoming_text=user_question,
            user_name=user_name,
            embedded_history=embedded_history,
        )
        
        logger.info(
            "generate_reply returned for dialog_id=%s: answer=%s, meta=%s",
            dialog_id,
            "None" if answer is None else f"length={len(answer)}",
            _meta
        )
        
    except Exception as e:
        logger.exception("Ошибка при генерации ответа: %s", e)
        answer = None
    
    # Если произошла ошибка при генерации ответа - не отправляем ничего клиенту
    if answer is None:
        logger.warning("Failed to generate reply for dialog_id=%s - not sending message to client", dialog_id)
        await message.answer("Извините, произошла техническая ошибка. Попробуйте позже или обратитесь к менеджеру.")
        return
    
    logger.info("Sending answer to Telegram user for dialog_id=%s, answer_length=%d", dialog_id, len(answer))
    
    # Создаем кнопки для рейтинга
    qa_id = f"{int(time.time() * 1000)}{random.randint(1000, 9999)}"
    TEMP_QA[qa_id] = {"question": user_question, "answer": answer}
    
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍", callback_data=f"rate_up|{qa_id}"),
                InlineKeyboardButton(text="👎", callback_data=f"rate_down|{qa_id}"),
            ]
        ]
    )
    
    await message.reply(answer, reply_markup=markup)


# ----------------------------
# Обработка обратной связи (👍 👎)
# ----------------------------
@user_router.callback_query(F.data.startswith("rate_"))
async def handle_rating(callback: CallbackQuery) -> None:
    """
    Обрабатывает обратную связь от пользователей (👍 👎).
    
    При положительном отзыве (👍) добавляет вопрос-ответ в FAQ.
    При отрицательном отзыве (👎) уведомляет о необходимости передачи менеджеру.
    
    Args:
        callback: Callback query с данными рейтинга
    """
    if not callback.data:
        return
    
    try:
        parts = callback.data.split("|")
        if len(parts) != 2:
            logger.warning("Invalid callback data format: %s", callback.data)
            await callback.answer("Ошибка обработки обратной связи.")
            return
        
        action, qa_id = parts
        qa_data = TEMP_QA.get(qa_id)
        
        if not qa_data:
            await callback.answer("Сообщение устарело, но кнопки останутся")
            return
        
        if action == "rate_up":
            # Загружаем существующий FAQ
            try:
                with open(FAQ_PATH, "r", encoding="utf-8") as f:
                    faq_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                faq_data = []
            
            # Проверяем, нет ли уже такого вопроса
            question = qa_data.get("question", "")
            if question and not any(item.get("question") == question for item in faq_data):
                faq_data.append(qa_data)
                try:
                    with open(FAQ_PATH, "w", encoding="utf-8") as f:
                        json.dump(faq_data, f, ensure_ascii=False, indent=2)
                    logger.info("Ответ добавлен в FAQ по положительному отзыву: qa_id=%s", qa_id)
                    await callback.answer("Ответ добавлен в базу знаний.")
                except Exception as e:
                    logger.exception("Ошибка при сохранении FAQ: %s", e)
                    await callback.answer("Ошибка при сохранении в базу знаний.")
            else:
                await callback.answer("Такой вопрос уже есть в базе знаний.")
        else:  # rate_down
            await callback.answer("Спасибо, передадим менеджеру.")
            
    except Exception as e:
        logger.exception("Ошибка при обработке оценки: %s", e)
        await callback.answer("Ошибка при обработке оценки.")
