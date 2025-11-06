"""
Модуль формирования промптов для LLM.

Содержит шаблоны и функции для построения промптов на основе
статического контекста, истории диалога, FAQ и дополнительных данных.
"""

# Системный промпт для LLM
SYSTEM_HINT: str = (
    "Ты — вежливый визовый помощник. "
    "Отвечай кратко и по делу, с примерами и шагами, когда уместно. "
    "ВАЖНО: Твой ответ не должен превышать 900 символов (ограничение Avito API). "
    "Будь лаконичен, но информативен. Если нужно много информации — выдели самое важное. "
    "Если информации недостаточно — укажи, что нужны уточнения. "
    "Если вопрос выходит за твои рамки или информации нет в FAQ/контексте — "
    "напиши фразу: 'По данному вопросу вам в ближайшее время ответит наш менеджер.' "
    "Эта фраза будет удалена из ответа клиенту, но используется для уведомления менеджера."
)

# Шаблон промпта
PROMPT_TEMPLATE: str = """{system_hint}

Необходимая информация о тарифах и услугах, типовые ответы на вопросы и описание профиля ассистента:
{static_context}

История переписки с клиентом:
{dialogue_context}

Найденные похожие вопросы и ответы из FAQ:
{faq_context}

Дополнительная вложенная история (если есть):
{embedded_history}

ВАЖНО: Если в FAQ или статическом контексте нет информации для ответа на вопрос клиента, 
или если вопрос выходит за рамки твоих знаний — ОБЯЗАТЕЛЬНО напиши фразу: 
"По данному вопросу вам в ближайшее время ответит наш менеджер."

Имя клиента: {user_name}
Последнее сообщение от клиента:
{incoming_text}
"""

# Значения по умолчанию для пустых полей
DEFAULT_STATIC_CONTEXT: str = "(нет)"
DEFAULT_DIALOGUE_CONTEXT: str = "(нет)"
DEFAULT_FAQ_CONTEXT: str = "(нет)"
DEFAULT_EMBEDDED_HISTORY: str = "(нет)"
DEFAULT_USER_NAME: str = "(неизвестно)"
DEFAULT_INCOMING_TEXT: str = "(пусто)"


def build_prompt(
    static_context: str,
    dialogue_context: str,
    faq_context: str,
    embedded_history: str,
    user_name: str | None,
    incoming_text: str
) -> str:
    """
    Формирует промпт для LLM на основе всех контекстов.
    
    Args:
        static_context: Статический контекст (информация о компании, услугах)
        dialogue_context: История диалога
        faq_context: Релевантные вопросы и ответы из FAQ
        embedded_history: Дополнительная вложенная история
        user_name: Имя пользователя (опционально)
        incoming_text: Входящий текст от пользователя
        
    Returns:
        Сформированный промпт для отправки в LLM
    """
    return PROMPT_TEMPLATE.format(
        system_hint=SYSTEM_HINT,
        static_context=static_context or DEFAULT_STATIC_CONTEXT,
        dialogue_context=dialogue_context or DEFAULT_DIALOGUE_CONTEXT,
        faq_context=faq_context or DEFAULT_FAQ_CONTEXT,
        embedded_history=embedded_history or DEFAULT_EMBEDDED_HISTORY,
        user_name=user_name or DEFAULT_USER_NAME,
        incoming_text=incoming_text or DEFAULT_INCOMING_TEXT,
    )
