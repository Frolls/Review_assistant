from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, Literal
from uuid import UUID

import tiktoken

from app.chat.domain import Chat, ChatMessage
from app.chat.prompts import default_system_prompt
from app.chat.repository import ChatRepository
from app.observability.logging import get_logger


logger = get_logger(__name__)

MessagePayload = dict[str, Any]
ContextStrategyName = Literal["sliding", "hybrid"]

DEFAULT_CONTEXT_WINDOW_TOKENS = 8_192
DEFAULT_RESPONSE_TOKENS = 1_536
DEFAULT_SAFETY_MARGIN = 256
DEFAULT_HISTORY_LIMIT = 200
SUMMARIZE_PROMPT = (
    "Сожми старую часть диалога для продолжения работы ассистента. "
    "Явно перечисли: темы, имена, числа, принятые решения и нерешенные вопросы. "
    "Не добавляй фактов, которых нет в истории."
)


class ChatNotFoundError(LookupError):
    pass


def count_tokens(messages: list[MessagePayload]) -> int:
    total = 2
    for message in messages:
        total += 4
        total += _count_text_tokens(message.get("role", ""))
        total += _count_content_tokens(message.get("content", ""))
    return total


def fit_to_budget(messages: list[MessagePayload], budget: int) -> list[MessagePayload]:
    if budget <= 0:
        return []
    if count_tokens(messages) <= budget:
        return messages

    preserved_system = [messages[0]] if messages and messages[0]["role"] == "system" else []
    rest = messages[1:] if preserved_system else messages[:]

    while rest and count_tokens([*preserved_system, *rest]) > budget:
        rest.pop(0)

    fitted = [*preserved_system, *rest]
    if fitted and count_tokens(fitted) <= budget:
        return fitted
    return preserved_system if preserved_system else fitted


class ChatService:
    def __init__(
        self,
        repository: ChatRepository,
        llm_client: Any,
        *,
        model: str = "gpt-4.1-mini",
        vision_model: str | None = None,
        num_ctx: int | None = None,
        context_strategy: ContextStrategyName = "sliding",
        keep_recent: int = 10,
        context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
        response_tokens: int = DEFAULT_RESPONSE_TOKENS,
        safety_margin: int = DEFAULT_SAFETY_MARGIN,
    ) -> None:
        self.repository = repository
        self.llm_client = llm_client
        self.model = model
        self.vision_model = vision_model
        self.num_ctx = num_ctx
        self.context_strategy = context_strategy
        self.keep_recent = keep_recent
        self.context_window_tokens = context_window_tokens
        self.response_tokens = response_tokens
        self.safety_margin = safety_margin

    async def create_chat(
        self,
        owner_external_id: str,
        interface: str,
        system_prompt: str | None = None,
    ) -> Chat:
        system_prompt = default_system_prompt(interface, system_prompt)
        return await self.repository.create_chat(owner_external_id, interface, system_prompt)

    async def get_chat(self, chat_id: UUID) -> Chat | None:
        return await self.repository.get_chat(chat_id)

    async def list_messages(self, chat_id: UUID, limit: int = 50) -> list[ChatMessage]:
        return await self.repository.list_messages(chat_id, limit)

    async def send_message(
        self,
        chat_id: UUID,
        user_content: str,
        media_ref: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        chat = await self.repository.get_chat(chat_id)
        if chat is None:
            raise ChatNotFoundError(f"Chat {chat_id} was not found")

        user_message = ChatMessage(
            chat_id=chat_id,
            role="user",
            content=user_content,
            media_refs=media_ref,
        )
        await self.repository.append_message(chat_id, user_message)

        history = await self.repository.list_messages(chat_id, limit=DEFAULT_HISTORY_LIMIT)
        messages = await self._build_context(chat, history)
        budget = self.context_window_tokens - self.response_tokens - self.safety_margin
        messages = fit_to_budget(messages, budget)

        accumulated: list[str] = []
        stream_completed = False
        try:
            stream = await self.llm_client.chat.completions.create(
                model=self._select_model(messages),
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                **self._completion_extra_kwargs(),
            )
            async for chunk in stream:
                text = _extract_delta_text(chunk)
                if not text:
                    continue
                accumulated.append(text)
                yield text
            stream_completed = True
        finally:
            full_text = "".join(accumulated)
            if full_text:
                await self.repository.append_message(
                    chat_id,
                    ChatMessage(
                        chat_id=chat_id,
                        role="assistant",
                        content=full_text,
                        tokens=count_tokens([{"role": "assistant", "content": full_text}]),
                    ),
                )
            if not stream_completed and full_text:
                logger.warning("chat.stream_interrupted_saved_partial", chat_id=str(chat_id))

    async def clear_history(self, chat_id: UUID) -> None:
        await self.repository.soft_delete_messages(chat_id)

    async def _build_context(
        self,
        chat: Chat,
        history: list[ChatMessage],
    ) -> list[MessagePayload]:
        if self.context_strategy == "hybrid":
            return await self._build_hybrid_context(chat, history)
        return self._build_sliding_context(chat, history)

    def _build_sliding_context(self, chat: Chat, history: list[ChatMessage]) -> list[MessagePayload]:
        messages = _system_messages(chat)
        messages.extend(_to_payload(message) for message in history[-self.keep_recent :])
        return messages

    async def _build_hybrid_context(
        self,
        chat: Chat,
        history: list[ChatMessage],
    ) -> list[MessagePayload]:
        messages = _system_messages(chat)
        old = history[: -self.keep_recent] if self.keep_recent > 0 else history
        recent = history[-self.keep_recent :] if self.keep_recent > 0 else []

        if old:
            summary = await self._summarize_old_messages(old)
            if summary:
                messages.append(
                    {
                        "role": "system",
                        "content": f"Краткая память старой части диалога:\n{summary}",
                    }
                )
        messages.extend(_to_payload(message) for message in recent)
        return messages

    async def _summarize_old_messages(self, messages: list[ChatMessage]) -> str:
        summary_input = "\n".join(
            f"{message.role}: {message.content}" for message in messages
        )
        response = await self.llm_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": summary_input},
            ],
            temperature=0,
            max_tokens=256,
        )
        return _extract_response_text(response).strip()

    def _select_model(self, messages: list[MessagePayload]) -> str:
        if self.vision_model and _messages_include_images(messages):
            return self.vision_model
        return self.model

    def _completion_extra_kwargs(self) -> dict[str, Any]:
        if self.num_ctx is None:
            return {}
        return {"extra_body": {"options": {"num_ctx": self.num_ctx}}}


def _system_messages(chat: Chat) -> list[MessagePayload]:
    if not chat.system_prompt:
        return []
    return [{"role": "system", "content": chat.system_prompt}]


def _to_payload(message: ChatMessage) -> MessagePayload:
    media_part = (message.media_refs or {}).get("part")
    if message.role == "user" and isinstance(media_part, dict):
        return {
            "role": message.role,
            "content": [
                {"type": "text", "text": message.content},
                media_part,
            ],
        }
    return {"role": message.role, "content": message.content}


def _messages_include_images(messages: list[MessagePayload]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return True
    return False


def _extract_delta_text(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    return _coerce_text(getattr(delta, "content", None))


def _extract_response_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    message = getattr(choices[0], "message", None) if choices else None
    return _coerce_text(getattr(message, "content", None))


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_coerce_text(item) for item in value)
    if isinstance(value, dict):
        return _coerce_text(value.get("content") or value.get("text"))
    return str(value)


def _count_content_tokens(value: Any) -> int:
    if isinstance(value, str):
        return _count_text_tokens(value)
    if isinstance(value, list):
        total = 0
        for part in value:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += _count_text_tokens(_coerce_text(part.get("text")))
            elif part.get("type") == "image_url":
                total += 256
        return total
    return _count_text_tokens(_coerce_text(value))


def _count_text_tokens(value: str) -> int:
    encoder = _token_encoder()
    if encoder is not None:
        return len(encoder(value))

    # Offline fallback for sandboxed tests when tiktoken cannot fetch its BPE
    # file yet. Production runs use tiktoken as soon as the encoding is cached.
    return max(1, len(value) // 4) if value else 0


@lru_cache(maxsize=1)
def _token_encoder():
    try:
        return tiktoken.get_encoding("o200k_base").encode
    except Exception as exc:  # pragma: no cover - depends on local tiktoken cache/network
        logger.warning("chat.token_encoder_unavailable", error=str(exc))
        return None
