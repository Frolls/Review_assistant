from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.chat.deps import get_chat_service
from app.chat.domain import Chat, ChatMessage
from app.chat.routes import router


class FakeChatService:
    def __init__(self) -> None:
        self.chat = Chat(owner_external_id="owner", interface="cli")
        self.messages = [
            ChatMessage(chat_id=self.chat.id, role="user", content="Hello"),
        ]

    async def create_chat(self, owner_external_id: str, interface: str, system_prompt: str | None = None):
        self.chat = Chat(
            owner_external_id=owner_external_id,
            interface=interface,
            system_prompt=system_prompt,
        )
        return self.chat

    async def get_chat(self, chat_id):
        return self.chat if chat_id == self.chat.id else None

    async def list_messages(self, chat_id, limit: int = 50):
        return self.messages[-limit:]

    async def send_message(self, chat_id, user_content: str) -> AsyncIterator[str]:
        yield "one "
        yield "two"

    async def clear_history(self, chat_id):
        self.messages.clear()


@pytest.fixture
def test_app() -> tuple[FastAPI, FakeChatService]:
    service = FakeChatService()
    app = FastAPI()
    app.include_router(router)

    async def override_chat_service() -> FakeChatService:
        return service

    app.dependency_overrides[get_chat_service] = override_chat_service
    return app, service


@pytest.mark.asyncio
async def test_create_chat_route(test_app):
    app, _ = test_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/chats",
            json={"owner_external_id": "owner-2", "interface": "cli"},
        )

    assert response.status_code == 200
    assert "chat_id" in response.json()


@pytest.mark.asyncio
async def test_stream_message_route(test_app):
    app, service = test_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/chats/{service.chat.id}/messages",
            json={"content": "Hi"},
        )

    assert response.status_code == 200
    assert response.text == "data: one \n\ndata: two\n\ndata: [DONE]\n\n"


@pytest.mark.asyncio
async def test_get_chat_and_messages_routes(test_app):
    app, service = test_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chat_response = await client.get(f"/chats/{service.chat.id}")
        messages_response = await client.get(f"/chats/{service.chat.id}/messages")

    assert chat_response.status_code == 200
    assert chat_response.json()["id"] == str(service.chat.id)
    assert messages_response.status_code == 200
    assert messages_response.json()[0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_get_chat_route_returns_404_for_unknown_chat(test_app):
    app, _ = test_app
    unknown_chat_id = "00000000-0000-0000-0000-000000000000"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/chats/{unknown_chat_id}")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_messages_route_passes_limit(test_app):
    app, service = test_app
    service.messages.append(
        ChatMessage(chat_id=service.chat.id, role="assistant", content="World")
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/chats/{service.chat.id}/messages?limit=1")

    assert response.status_code == 200
    assert [message["content"] for message in response.json()] == ["World"]


@pytest.mark.asyncio
async def test_clear_messages_route(test_app):
    app, service = test_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/chats/{service.chat.id}/messages")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert service.messages == []
