"""
Модуль формирования промптов для LLM.

Содержит шаблоны и функции для построения промптов на основе
статического контекста, динамического контекста, системного промпта,
истории диалога, FAQ и дополнительных данных.
"""
import logging

logger = logging.getLogger(__name__)

# Жестко зашитая часть системного промпта (всегда присутствует)
HARDCODED_SYSTEM_PART: str = (
    "[ПРИОРИТЕТЫ]\n\n"
    "P0 FAQ_MATCH: Если вопрос пользователя совпал (точно или ~≥0.9 схожести после нормализации) "
    "с вопросом из FAQ, выведи строго поле «Ответ» для этого вопроса. "
    "НИЧЕГО не добавляй и не перефразируй. Разрешено только: обрезать текст до 950 символов.\n\n"
    "P1 DYNAMIC: Если FAQ_MATCH не сработал, отвечай по динамической информации.\n\n"
    "P2 STYLE: Применяй стиль (вежливо, без длинных тире и звездочек), но НЕ изменяй факты P0/P1.\n\n"
    "[СРАВНЕНИЕ]\n"
    "- Перед сравнением сними ссылки/ментIONS, приведи к нижнему регистру, убери пунктуацию и лишние пробелы.\n"
    "- Если нашёл более одного кандидата, возьми с max score.\n\n"
    "[ЕСЛИ НЕТ ДАННЫХ]\n"
    "- Если в FAQ и динамике нет ответа: выведи ровно «По данному вопросу вам в ближайшее время ответит наш менеджер.»\n\n"
    "ВАЖНО: Твой ответ не должен превышать 950 символов (ограничение Avito API). "
    "Ты — вежливый визовый помощник. Будь лаконичен, но информативен. "
    "Убирай звездочки из ответов, телеграм их не понимает."
)


# Шаблон промпта
PROMPT_TEMPLATE: str = """{system_prompt}

{hardcoded_system_part}

Статическая информация о компании, услугах и профиле ассистента:
{static_context}

Динамическая информация о тарифах, услугах, стоимостях (актуальная на сегодня):
{dynamic_context}

История переписки с клиентом (последние сообщения):
{dialogue_context}

Найденные похожие вопросы и ответы из FAQ:
{faq_context}

ВАЖНО: 
- Если P0 сработал (найден точный матч FAQ), данные из «Динамики» игнорируются, даже если противоречат FAQ.
- Если в FAQ и динамике нет информации для ответа на вопрос клиента, 
  или если вопрос выходит за рамки твоих знаний — ОБЯЗАТЕЛЬНО напиши фразу: 
  "По данному вопросу вам в ближайшее время ответит наш менеджер."

Имя клиента: {user_name}
Последний вопрос от клиента на который ты должен ответить:
{incoming_text}
"""

# Значения по умолчанию для пустых полей
DEFAULT_STATIC_CONTEXT: str = "(нет)"
DEFAULT_DIALOGUE_CONTEXT: str = "(нет)"
DEFAULT_FAQ_CONTEXT: str = "(нет)"
DEFAULT_USER_NAME: str = "(неизвестно)"
DEFAULT_INCOMING_TEXT: str = "(пусто)"


def build_prompt(
    system_prompt: str,
    static_context: str,
    dynamic_context: str,
    dialogue_context: str,
    faq_context: str,
    user_name: str | None,
    incoming_text: str
) -> str:
    """
    Формирует промпт для LLM на основе всех контекстов.
    
    Args:
        system_prompt: Системный промпт из файла (описание манеры поведения ассистента)
        static_context: Статический контекст (информация о компании, услугах)
        dynamic_context: Динамический контекст (тарифы, услуги, стоимости)
        dialogue_context: История диалога из chat_history.json (последние сообщения)
        faq_context: Релевантные вопросы и ответы из FAQ
        user_name: Имя пользователя (опционально)
        incoming_text: Входящий текст от пользователя
        
    Returns:
        Сформированный промпт для отправки в LLM
    """
    # Используем системный промпт из файла, если он есть, иначе - пустая строка
    # Жестко зашитая часть про 950 символов всегда добавляется отдельно
    system_hint = system_prompt.strip() if system_prompt.strip() else ""
    
    prompt = PROMPT_TEMPLATE.format(
        system_prompt=system_hint,
        hardcoded_system_part=HARDCODED_SYSTEM_PART,
        static_context=static_context or DEFAULT_STATIC_CONTEXT,
        dynamic_context=dynamic_context or "(нет)",
        dialogue_context=dialogue_context or DEFAULT_DIALOGUE_CONTEXT,
        faq_context=faq_context or DEFAULT_FAQ_CONTEXT,
        user_name=user_name or DEFAULT_USER_NAME,
        incoming_text=incoming_text or DEFAULT_INCOMING_TEXT,
    )
    
    # Выводим итоговый промпт в консоль для отладки
    print("\n" + "=" * 80)
    print("ИТОГОВЫЙ ПРОМПТ ДЛЯ LLM:")
    print("=" * 80)
    print(prompt)
    print("=" * 80 + "\n")
    
    return prompt
