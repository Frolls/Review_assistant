from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import access


@pytest.mark.asyncio
async def test_chat_owner_cannot_be_forged_with_only_user_header(monkeypatch):
    chat_id = uuid4()
    class Repo:
        async def get_chat(self, value):
            return SimpleNamespace(owner_external_id="alice") if value == chat_id else None
    async def repository(request):
        yield Repo()
    monkeypatch.setattr(access, "get_repository", repository)
    app = FastAPI()
    app.state.settings = SimpleNamespace(internal_token="trusted-adapter", admin_token="admin")
    app.middleware("http")(access.access_middleware)
    @app.get("/chats/{chat_id}")
    async def chat(chat_id: str):
        return {"ok": True}
    @app.post("/chats")
    async def create():
        return {"ok": True}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/chats/{chat_id}"
        assert (await client.get(url, headers={"X-User-ID": "alice"})).status_code == 401
        auth = {"X-Internal-Token": "trusted-adapter", "X-User-ID": "bob"}
        assert (await client.get(url, headers=auth)).status_code == 404
        assert (await client.post("/chats", headers=auth, json={"owner_external_id": "alice"})).status_code == 403
        auth["X-User-ID"] = "alice"
        assert (await client.get(url, headers=auth)).status_code == 200
