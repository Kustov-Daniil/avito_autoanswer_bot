import os
import random
import time
import json
import logging
import re
import difflib
import httpx
from aiogram import F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.chat_action import ChatActionSender
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import AsyncOpenAI
from bs4 import BeautifulSoup

from create_bot import bot
from config import FAQ_PATH, STATIC_CONTEXT_PATH, CHAT_HISTORY_PATH, LLM_MODEL, TEMPERATURE, OPENAI_API_KEY, ADMINS
from responder import generate_reply
from config import WEBHOOK_URL
from avito_api import subscribe_webhook, unsubscribe_webhook

# ----------------------------
# Инициализация
# ----------------------------
user_router = Router()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

# Create httpx client explicitly to avoid proxy-related issues
http_client = httpx.AsyncClient()
client = AsyncOpenAI(api_key=OPENAI_API_KEY, http_client=http_client)
os.makedirs("data", exist_ok=True)

# Инициализация файлов
if not os.path.exists(FAQ_PATH):
    with open(FAQ_PATH, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)
if not os.path.exists(STATIC_CONTEXT_PATH):
    with open(STATIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
        f.write("")

TEMP_QA = {}

class AdminStates(StatesGroup):
    waiting_for_faq_file = State()
    waiting_for_static_context = State()



# ----------------------------
# /start
# ----------------------------
@user_router.message(CommandStart())
async def cmd_start(message: Message):
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        text = (
            "Привет! Я визовый помощник.\n"
            "Помогу с документами, визами и подачей заявлений.\n"
            "Задай вопрос — я постараюсь ответить максимально точно."
        )
        await message.answer(text)
        logger.info(f"/start вызван пользователем {message.from_user.id}")

# ----------------------------
# /uploadfaq — админ
# ----------------------------
@user_router.message(F.text.startswith("/uploadfaq"))
async def cmd_upload_faq(message: Message, state: FSMContext):
    if message.from_user.id not in ADMINS:
        logger.warning(f"Неавторизованный пользователь {message.from_user.id} попытался загрузить FAQ")
        return await message.answer("⛔️ У вас нет прав для загрузки FAQ.")
    await message.answer("Отправьте файл FAQ (txt, html или csv)")
    await state.set_state(AdminStates.waiting_for_faq_file)

# ----------------------------
# /setcontext — админ
# ----------------------------
@user_router.message(F.text.startswith("/setcontext"))
async def cmd_set_context(message: Message, state: FSMContext):
    if message.from_user.id not in ADMINS:
        logger.warning(f"Неавторизованный пользователь {message.from_user.id} попытался установить контекст")
        return await message.answer("⛔️ У вас нет прав для загрузки контекста.")
    await message.answer("Отправьте новый текст статичного контекста (он перезапишет старый).")
    await state.set_state(AdminStates.waiting_for_static_context)

@user_router.message(AdminStates.waiting_for_static_context)
async def handle_static_context(message: Message, state: FSMContext):
    try:
        new_context = message.text.strip()
        with open(STATIC_CONTEXT_PATH, "w", encoding="utf-8") as f:
            f.write(new_context)
        logger.info("Статичный контекст успешно обновлен.")
        await message.answer("Контекст обновлён.")
    except Exception as e:
        logger.error(f"Ошибка при сохранении контекста: {e}")
        await message.answer("Ошибка при сохранении контекста.")
    await state.clear()



@user_router.message(F.text.regexp(r"^/subscribe\b"))
async def tg_subscribe(message: Message):
    if message.from_user.id not in ADMINS:
        return await message.answer("⛔️ Недостаточно прав.")
    if not WEBHOOK_URL:
        return await message.answer("❗️Не задан PUBLIC_BASE_URL в .env")
    ok = subscribe_webhook(WEBHOOK_URL)
    await message.answer("✅ Вебхук зарегистрирован." if ok else "❌ Ошибка регистрации вебхука.")

@user_router.message(F.text.regexp(r"^/unsubscribe\b"))
async def tg_unsubscribe(message: Message):
    if message.from_user.id not in ADMINS:
        return await message.answer("⛔️ Недостаточно прав.")
    if not WEBHOOK_URL:
        return await message.answer("❗️Не задан PUBLIC_BASE_URL в .env")
    ok = unsubscribe_webhook(WEBHOOK_URL)
    await message.answer("✅ Вебхук отключён." if ok else "❌ Ошибка отключения вебхука.")

# ----------------------------
# Обработка FAQ файла
# ----------------------------
@user_router.message(AdminStates.waiting_for_faq_file, F.document)
async def handle_faq_file(message: Message, state: FSMContext):
    document = message.document
    file_path = os.path.join("data", f"faq_upload_{int(time.time())}_{document.file_name}")
    await bot.download(file=document.file_id, destination=file_path)
    logger.info(f"Админ {message.from_user.id} загрузил файл FAQ: {file_path}")

    # Чтение файла
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
        logger.error(f"Ошибка при чтении файла: {e}")
        await message.answer("Ошибка при чтении файла FAQ.")
        await state.clear()
        return

    # Разделяем контент на части по ~6000 символов
    chunk_size = 6000
    chunks = [new_content[i:i + chunk_size] for i in range(0, len(new_content), chunk_size)]
    logger.info(f"Файл разделен на {len(chunks)} частей")

    with open(FAQ_PATH, "r", encoding="utf-8") as f:
        try:
            current_faq = json.load(f)
        except json.JSONDecodeError:
            current_faq = []

    all_new_faq = []
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
            response = await client.chat.completions.create(
                model=os.getenv("LLM_MODEL", LLM_MODEL),
                messages=[{"role": "user", "content": prompt}],
                temperature=float(os.getenv("TEMPERATURE", 0.2)),
            )
            llm_response = response.choices[0].message.content.strip()
            match = re.search(r"\[.*\]", llm_response, re.DOTALL)
            if match:
                chunk_faq = json.loads(match.group(0))
                chunk_faq = [
                    {"question": i.get("question", "").strip(), "answer": i.get("answer", "").strip()}
                    for i in chunk_faq if isinstance(i, dict)
                ]
                all_new_faq.extend(chunk_faq)
                logger.info(f"Обработана часть {idx}/{len(chunks)}, получено {len(chunk_faq)} записей.")
            else:
                logger.warning(f"Часть {idx} не вернула корректный JSON")
        except Exception as e:
            logger.error(f"Ошибка LLM при обработке части {idx}: {e}")
            continue

    # Убираем дубли
    questions_existing = {item['question'] for item in current_faq}
    combined_faq = current_faq + [item for item in all_new_faq if item['question'] and item['question'] not in questions_existing]

    # Сохраняем
    with open(FAQ_PATH, "w", encoding="utf-8") as f:
        json.dump(combined_faq, f, ensure_ascii=False, indent=2)

    logger.info(f"FAQ обновлен. Добавлено {len(all_new_faq)} новых записей.")
    await message.answer(f"FAQ обновлен. Добавлено {len(all_new_faq)} записей.")
    await state.clear()

# ----------------------------
# Ответ пользователю в ТГ (через единый responder)
# ----------------------------
@user_router.message(F.text)
async def handle_user_message(message: Message):
    logger.info(f"handle_user_message called: user_id={message.from_user.id}, text_length={len(message.text) if message.text else 0}")
    raw_text = message.text.strip()
    # Игнор системных сообщений
    if raw_text.startswith("Системное:") or raw_text.startswith("Сообщение отправлено"):
        logger.debug("Skipping system message")
        return

    # Диалоговый id
    m = re.search(r";([0-9]+:m:[^:]+):", raw_text)
    dialog_id = m.group(1) if m else f"tg_{message.from_user.id}"

    # Чистим хвост id
    clean_text = re.sub(r"[a-z0-9]+;[0-9]+:m:[^:]+:[0-9]+$", "", raw_text).strip()

    # Имя и вопрос + вложенная история
    name_m = re.search(r"^([\\wА-Яа-яёЁ]+):\\s*(.+)", clean_text)
    if name_m:
        user_name = name_m.group(1)
        user_question = name_m.group(2).strip()
    else:
        user_name = None
        user_question = clean_text

    hist_m = re.search(r"ИСТОРИЯ(.+)", clean_text, re.DOTALL)
    embedded_history = hist_m.group(1).strip() if hist_m else ""

    # Генерация ответа единым модулем
    answer, _meta = await generate_reply(
        dialog_id=dialog_id,
        incoming_text=user_question,
        user_name=user_name,
        embedded_history=embedded_history,
    )

    # Рейтинг
    qa_id = str(int(time.time() * 1000)) + str(random.randint(1000, 9999))
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
async def handle_rating(callback: CallbackQuery):
    try:
        action, qa_id = callback.data.split("|")
        qa_data = TEMP_QA.get(qa_id)

        if not qa_data:
            await callback.answer("Сообщение устарело, но кнопки останутся")
            return

        if action == "rate_up":
            with open(FAQ_PATH, "r", encoding="utf-8") as f:
                try:
                    faq_data = json.load(f)
                except json.JSONDecodeError:
                    faq_data = []
            if not any(item.get("question") == qa_data["question"] for item in faq_data):
                faq_data.append(qa_data)
                with open(FAQ_PATH, "w", encoding="utf-8") as f:
                    json.dump(faq_data, f, ensure_ascii=False, indent=2)
            await callback.answer("Ответ добавлен в базу знаний.")
        else:
            await callback.answer("Спасибо, передадим менеджеру.")
    except Exception as e:
        logging.exception(e)
        await callback.answer("Ошибка при обработке оценки.")

