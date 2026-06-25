from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.chat.domain import Chat, ChatMessage
from app.chat.service import ChatService, count_tokens, fit_to_budget


class FakeRepository:
    def __init__(self, chat: Chat) -> None:
        self.chat = chat
        self.messages: list[ChatMessage] = []

    async def create_chat(self, owner_external_id: str, interface: str, system_prompt: str | None = None):
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

    fitted = fit_to_budget(messages, budget)

    assert fitted == [messages[0]]


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
