from unittest.mock import AsyncMock
import asyncio

from fastapi import FastAPI, Request
from httpx import AsyncClient, ASGITransport
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from app.routers.agent import router
from app.services.agent_persistent import build_agent


class Model:
    def bind_tools(self, tools):
        return self
    async def ainvoke(self, messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content="Done")
        return AIMessage(content="", tool_calls=[{"name": "send_telegram_message", "id": "call", "args": {"chat_id": 1001, "text": "test"}}])


@pytest.mark.asyncio
async def test_pending_actions_are_owner_bound_and_cannot_be_resumed_twice():
    sender = AsyncMock()
    app = FastAPI()
    app.state.llm_semaphore = asyncio.Semaphore(5)
    app.state.agent_graph = build_agent(InMemorySaver(), model=Model(), sender=sender)
    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.owner_id = request.headers.get("X-User-ID", "1001")
        return await call_next(request)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {"thread_id": "telegram-actions", "input": {"messages": [{"role": "user", "content": "Send"}]}}
        response = await client.post("/agent/stream", json=payload)
        assert response.status_code == 200 and '"event":"paused"' in response.text
        pending = (await client.get("/agent/pending")).json()["pending"]
        assert (await client.get("/agent/pending", headers={"X-User-ID": "2002"})).json()["pending"] is None
        assert (await client.post("/agent/stream", json=payload)).status_code == 409
        resume = {"thread_id": "telegram-actions", "resume": False, "request_id": pending["request_id"]}
        assert (await client.post("/agent/stream", json=resume, headers={"X-User-ID": "2002"})).status_code == 409
        assert (await client.post("/agent/stream", json=resume)).status_code == 200
        assert (await client.post("/agent/stream", json=resume)).status_code == 409
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_both_agent_endpoints_share_the_chat_semaphore_and_release_on_errors():
    from types import SimpleNamespace

    class Semaphore(asyncio.Semaphore):
        def __init__(self):
            super().__init__(1)
            self.waiters = 0
            self.both_waiting = asyncio.Event()
        async def acquire(self):
            if self.locked():
                self.waiters += 1
                if self.waiters == 2:
                    self.both_waiting.set()
            return await super().acquire()

    semaphore = Semaphore()
    active = 0
    peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0
    async def generation():
        nonlocal active, peak, count
        active += 1
        peak = max(peak, active)
        count += 1
        entered.set()
        try:
            await release.wait()
            raise RuntimeError("Model failed")
        finally:
            active -= 1
    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(values={}, next=(), tasks=())
        async def astream(self, *args, **kwargs):
            await generation()
            yield "updates", {}
        async def ainvoke(self, *args, **kwargs):
            await generation()

    app = FastAPI()
    app.state.llm_semaphore = semaphore
    app.state.agent_graph = Graph()
    app.state.multi_agent_graph = Graph()
    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.owner_id = "1001"
        return await call_next(request)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        # Occupy the same semaphore used by ChatService; neither graph may start.
        async with semaphore:
            streaming = asyncio.create_task(client.post("/agent/stream", json={
                "thread_id": "capacity", "input": {"messages": [{"role": "user", "content": "time"}]}}))
            review = asyncio.create_task(client.post("/agent/review", json={"question": "Review this code"}))
            await asyncio.wait_for(semaphore.both_waiting.wait(), 3)
            assert count == 0
        await asyncio.wait_for(entered.wait(), 3)
        assert active == 1
        release.set()
        stream_response, review_response = await asyncio.wait_for(asyncio.gather(streaming, review), 3)
    assert '"event":"error"' in stream_response.text
    assert review_response.status_code == 500
    assert count == 2 and peak == 1 and active == 0
    assert not semaphore.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["model", "after_tool", "prepare"])
async def test_new_request_recovers_failed_turn_without_losing_completed_history(failure):
    from hashlib import sha256
    from langchain_core.messages import HumanMessage

    failed_question = "Отправь сообщение другому получателю" if failure == "prepare" else "Failed question"

    class RecoveringModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            question = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
            if question == "Earlier question":
                return AIMessage(content="Earlier answer")
            if question == failed_question:
                if failure == "model" or isinstance(messages[-1], ToolMessage):
                    raise TimeoutError("Model timed out")
                tool = "send_telegram_message" if failure == "prepare" else "get_current_time"
                args = {"chat_id": 9999, "text": "Wrong recipient"} if failure == "prepare" else {"timezone": "Asia/Yekaterinburg"}
                return AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": "failed-tool"}])
            assert all(m.content != failed_question for m in messages)
            assert any(m.content == "Earlier answer" for m in messages)
            if isinstance(messages[-1], HumanMessage):
                return AIMessage(content="", tool_calls=[{
                    "name": "get_current_time", "args": {"timezone": "Asia/Yekaterinburg"}, "id": "retry-clock",
                }])
            return AIMessage(content="Recovered answer")

    sender = AsyncMock()
    graph = build_agent(InMemorySaver(), model=RecoveringModel(), sender=sender)
    app = FastAPI()
    app.state.llm_semaphore = asyncio.Semaphore(1)
    app.state.agent_graph = graph

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.owner_id = "1001"
        return await call_next(request)

    app.include_router(router)
    thread = "telegram-actions"
    config = {"configurable": {"thread_id": sha256(f"1001:{thread}".encode()).hexdigest()}}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async def ask(text):
            return await client.post("/agent/stream", json={
                "thread_id": thread, "input": {"messages": [{"role": "user", "content": text}]},
            })

        assert '"event":"done"' in (await ask("Earlier question")).text
        failed = await ask(failed_question)
        assert '"event":"error"' in failed.text
        assert (await graph.aget_state(config)).next
        assert (await client.get("/agent/pending")).json()["pending"] is None

        recovered = await ask("Time again")
        assert recovered.status_code == 200
        assert '"event":"done"' in recovered.text
        assert "Recovered answer" in recovered.text
        assert "get_current_time" in recovered.text
        state = await graph.aget_state(config)
        assert not state.next
        assert [m.content for m in state.values["messages"] if isinstance(m, HumanMessage)] == [
            "Earlier question", "Time again",
        ]
        # The failed attempt remains in checkpoint history for diagnosis.
        history = [s async for s in graph.aget_state_history(config)]
        assert any(any(m.content == failed_question for m in s.values.get("messages", [])) for s in history)
    sender.assert_not_called()
