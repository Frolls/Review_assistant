from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.chat.domain import Chat, ChatMessage
from app.chat.prompts import TELEGRAM_SYSTEM_PROMPT
from app.chat.service import ChatService, count_tokens, fit_to_budget


class FakeRepository:
    def __init__(self, chat: Chat) -> None:
        self.chat = chat
        self.messages: list[ChatMessage] = []
        self.create_chat_calls: list[dict[str, str | None]] = []

    async def create_chat(self, owner_external_id: str, interface: str, system_prompt: str | None = None):
        self.create_chat_calls.append(
            {
                "owner_external_id": owner_external_id,
                "interface": interface,
                "system_prompt": system_prompt,
            }
        )
        return self.chat

    async def get_chat(self, chat_id: UUID):
        return self.chat if chat_id == self.chat.id else None

    async def append_message(self, chat_id: UUID, message: ChatMessage):
        self.messages.append(message)
        return message

    async def list_messages(self, chat_id: UUID, limit: int = 50):
        return self.messages[-limit:]

    async def soft_delete_messages(self, chat_id: UUID):
        self.messages.clear()


class FakeStream:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            content = next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=content))]
        )


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return FakeStream(["Hello, ", "Anya"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))]
        )


class FakeLLM:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions())


def test_fit_to_budget_preserves_first_system_message():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "x" * 200},
        {"role": "assistant", "content": "y" * 200},
    ]
    budget = count_tokens([messages[0]]) + 1

    with pytest.raises(ValueError, match="превышает контекст"):
        fit_to_budget(messages, budget)


@pytest.mark.asyncio
async def test_sliding_context_keeps_system_prompt_and_recent_messages():
    chat = Chat(owner_external_id="owner", interface="cli", system_prompt="System.")
    repo = FakeRepository(chat)
    llm = FakeLLM()
    service = ChatService(repo, llm, keep_recent=2)
    history = [
        ChatMessage(chat_id=chat.id, role="user", content="old"),
        ChatMessage(chat_id=chat.id, role="assistant", content="middle"),
        ChatMessage(chat_id=chat.id, role="user", content="recent"),
    ]

    context = service._build_sliding_context(chat, history)

    assert [message["content"] for message in context] == ["System.", "middle", "recent"]


@pytest.mark.asyncio
async def test_create_chat_applies_telegram_domain_system_prompt():
    chat = Chat(owner_external_id="owner", interface="telegram")
    repo = FakeRepository(chat)
    service = ChatService(repo, FakeLLM())

    await service.create_chat("owner", "telegram")

    assert repo.create_chat_calls[-1]["system_prompt"] == TELEGRAM_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_hybrid_context_summarizes_old_messages():
    chat = Chat(owner_external_id="owner", interface="cli", system_prompt="System.")
    repo = FakeRepository(chat)
    llm = FakeLLM()
    service = ChatService(repo, llm, context_strategy="hybrid", keep_recent=1)
    history = [
        ChatMessage(chat_id=chat.id, role="user", content="old"),
        ChatMessage(chat_id=chat.id, role="user", content="new"),
    ]

    context = await service._build_context(chat, history)

    assert [message["role"] for message in context] == ["system", "system", "user"]
    assert "summary" in context[1]["content"]


@pytest.mark.asyncio
async def test_send_message_streams_and_saves_assistant_response():
    chat = Chat(owner_external_id="owner", interface="cli")
    repo = FakeRepository(chat)
    llm = FakeLLM()
    service = ChatService(repo, llm, keep_recent=10)

    chunks = [chunk async for chunk in service.send_message(chat.id, "Hi")]

    assert chunks == ["Hello, ", "Anya"]
    assert [message.role for message in repo.messages] == ["user", "assistant"]
    assert repo.messages[-1].content == "Hello, Anya"


@pytest.mark.asyncio
async def test_send_message_uses_vision_model_for_image_history():
    chat = Chat(owner_external_id="owner", interface="cli")
    repo = FakeRepository(chat)
    llm = FakeLLM()
    service = ChatService(repo, llm, model="text-model", vision_model="vision-model")

    chunks = [
        chunk
        async for chunk in service.send_message(
            chat.id,
            "[фото]",
            media_ref={
                "mime": "image/png",
                "part": {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAA="},
                },
            },
        )
    ]

    assert chunks == ["Hello, ", "Anya"]
    assert llm.chat.completions.calls[-1]["model"] == "vision-model"


@pytest.mark.asyncio
async def test_blocked_output_never_reaches_client():
    from unittest.mock import AsyncMock
    from app.moderation import ModerationResult
    chat = Chat(owner_external_id="owner", interface="cli")
    repo = FakeRepository(chat)
    moderator = SimpleNamespace(
        check_input=AsyncMock(return_value=ModerationResult(True, [], [], "")),
        check_output=AsyncMock(return_value=ModerationResult(False, ["test"], [], "test")),
    )
    service = ChatService(repo, FakeLLM(), moderation_service=moderator)
    output = "".join([part async for part in service.send_message(chat.id, "Hi")])
    assert "Hello" not in output and "Anya" not in output
    assert output == repo.messages[-1].content
    moderator.check_output.assert_awaited_once_with("Hello, Anya")


@pytest.mark.asyncio
async def test_file_followup_uses_local_context_without_rag_refusal():
    from unittest.mock import AsyncMock
    chat = Chat(owner_external_id="owner", interface="cli")
    repo = FakeRepository(chat)
    rag = SimpleNamespace(prepare=AsyncMock(side_effect=AssertionError("Should analyze the attached code")))
    llm = FakeLLM()
    service = ChatService(repo, llm, rag_service=rag)
    file = {"part": {"type": "text", "text": "def add(x, y): return x + y"}}
    _ = [p async for p in service.send_message(chat.id, "Объясни", media_ref=file)]
    _ = [p async for p in service.send_message(chat.id, "Какие тесты добавить?")]
    assert "def add" in str(llm.chat.completions.calls[-1]["messages"])
    rag.prepare.assert_not_called()


def test_budget_preserves_latest_request_and_all_system_instructions():
    rules = {"role": "system", "content": "Grounding rules"}
    current = {"role": "user", "content": "def f(): return 1"}
    old = {"role": "user", "content": "old " * 500}
    assert fit_to_budget([rules, old, current], count_tokens([rules, current])) == [rules, current]
