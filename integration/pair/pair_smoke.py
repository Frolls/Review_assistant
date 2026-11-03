"""Real Telegram backend client → API; deterministic model, no external send."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from app.core.config import Settings
from app.deps.providers import get_cache, get_rag_service
from app.routers.agent import router as agent_router
from app.routers.health import router as health_router
from app.routers.rag import router as rag_router
from app.services.agent_persistent import build_agent
from app.services.rag import RAGService
from bot.services.backend_client import BackendClient


@pytest.mark.asyncio
async def test_readiness_cited_rag_preview_cancel():
    app = FastAPI()
    settings = Settings(OPENAI_API_KEY="test", CHAT_REPOSITORY="json", RAG_RERANKER_ENABLED=False)
    rag = RAGService(settings)
    node = SimpleNamespace(score=.9, node=SimpleNamespace(text="Mutable defaults persist across calls.", metadata={"file_name": "python.md"}))
    rag._retriever = SimpleNamespace(retrieve=lambda query: [node])
    async def completion(**kwargs):
        text = '{"decision":"answer"}' if kwargs.get("response_format") else "Use None [1]."
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
    await rag._llm.close()
    rag._llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    class Model:
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages):
            return AIMessage(content="", tool_calls=[{"name": "send_telegram_message", "id": "send", "args": {"chat_id": 1001, "text": "review complete"}}])
    sender = AsyncMock()
    app.state.settings = settings
    app.state.llm_semaphore = asyncio.Semaphore(2)
    app.state.rag_service = rag
    app.state.agent_graph = build_agent(InMemorySaver(), model=Model(), sender=sender, settings=settings)
    app.state.vector_store = SimpleNamespace(client=SimpleNamespace(get_collection=AsyncMock()))
    app.state.openai = SimpleNamespace(models=SimpleNamespace(list=AsyncMock()))
    app.dependency_overrides[get_cache] = lambda: SimpleNamespace(ping=AsyncMock())
    app.dependency_overrides[get_rag_service] = lambda: rag
    @app.middleware("http")
    async def identify(request: Request, call_next):
        request.state.owner_id = request.headers["X-User-ID"]
        return await call_next(request)
    for router in (health_router, rag_router, agent_router):
        app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://backend") as http:
        backend = BackendClient("http://backend", client=http, internal_token="test", owner_id="1001")
        assert (await http.get("/ready", headers=backend._headers())).json()["status"] == "ok"
        answer = (await http.post("/rag/query", headers=backend._headers(), json={"question": "Mutable default?"})).json()
        assert answer["confident"] and answer["sources"][0]["id"] == 1 and "[1]" in answer["answer"]
        progress = []
        async def update(event):
            progress.append(event)
        events = await backend.agent({"thread_id": "telegram-actions", "input": {"messages": [{"role": "user", "content": "Send review complete"}]}}, on_progress=update)
        assert any(e["event"] == "paused" for e in events)
        pending = await backend.pending_action()
        assert pending["chat_id"] == 1001 and pending["text"] == "review complete"
        cancelled = await backend.agent({"thread_id": "telegram-actions", "resume": False, "request_id": pending["request_id"]})
        assert any(e["event"] == "done" for e in cancelled)
        assert await backend.pending_action() is None
        assert any(e["event"] == "updates" for e in progress)
    sender.assert_not_called()
