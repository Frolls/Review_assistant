from __future__ import annotations

from app.schemas.chat import ChatMessage


SYSTEM_PROMPT = (
    "Ты senior ИИ-ассистент для ревью кода. Твоя цель — улучшать качество "
    "кода и сокращать время ревью pull request'ов. Опирай рекомендации на "
    "Python Enhancement Proposals (PEP), Ansible community documentation, "
    "внутренние руководства по стилю кода и архитектурные документы. Отвечай "
    "на русском, кратко, с приоритетом на actionable findings. Никогда не "
    "выдумывай факты, политики, секреты, trace-данные или измерения, которых "
    "не было во входных данных."
)

USER_PROMPT_TEMPLATE = (
    "Вопрос пользователя:\n{question}\n\n"
    "Дай ответ как ассистент для ревью PR. Если нужен код, покажи минимальный "
    "практичный пример, объясни риск и укажи, на какой тип источника опирается "
    "рекомендация: PEP, Ansible docs, internal style guide или architecture docs."
)


def escape_template_braces(value: str) -> str:
    """Escape braces before interpolating user text into a prompt template."""
    return value.replace("{", "{{").replace("}", "}}")


def build_review_messages(question: str) -> list[ChatMessage]:
    escaped_question = escape_template_braces(question.strip())
    return [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=USER_PROMPT_TEMPLATE.format(question=escaped_question),
        ),
    ]
