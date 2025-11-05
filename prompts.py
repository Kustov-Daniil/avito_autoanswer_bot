# prompts.py

SYSTEM_HINT = (
    "Ты — вежливый и точный визовый помощник. "
    "Отвечай кратко и по делу, с примерами и шагами, когда уместно. "
    "Если информации недостаточно — укажи, что нужны уточнения. "
    "Если вопрос выходит за твои рамки — скажи, что передашь менеджеру."
)

PROMPT_TEMPLATE = """{system_hint}

Статический контекст:
{static_context}

История переписки:
{dialogue_context}

Контекст из FAQ:
{faq_context}

Дополнительная вложенная история (если есть):
{embedded_history}

Имя клиента: {user_name}
Сообщение клиента:
{incoming_text}
"""

def build_prompt(static_context: str,
                 dialogue_context: str,
                 faq_context: str,
                 embedded_history: str,
                 user_name: str | None,
                 incoming_text: str) -> str:
    return PROMPT_TEMPLATE.format(
        system_hint=SYSTEM_HINT,
        static_context=static_context or "(нет)",
        dialogue_context=dialogue_context or "(нет)",
        faq_context=faq_context or "(нет)",
        embedded_history=embedded_history or "(нет)",
        user_name=user_name or "(неизвестно)",
        incoming_text=incoming_text or "(пусто)",
    )
