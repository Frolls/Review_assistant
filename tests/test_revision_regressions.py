import asyncio
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.observability import privacy, tracing
from app.observability.logging import content_privacy_processor
from app.services.agent_persistent import context_removals
from app.services.model_adapter import ModelAdapter


def test_every_custom_span_attribute_and_log_respects_content_flag(monkeypatch):
    monkeypatch.setattr(privacy, "_include_content", False)
    secret = "private_source_code_UNIQUE"
    exported = {}
    span = SimpleNamespace(set_attribute=lambda k, v: exported.update({k: v}))
    def start(name, attributes):
        exported.update(attributes)
        return span
    monkeypatch.setattr(tracing, "_TRACER", SimpleNamespace(start_span=start))
    monkeypatch.setattr(tracing, "_use_span", lambda s: nullcontext(s))
    with tracing.rag_span("rag.test", **{"input.value": secret, "rag.model": "test"}) as active:
        active.set_attribute("output.value", secret)
        active.set_attributes({"arbitrary": {"nested": [secret]}, "count": 2})
    assert secret not in str(exported)
    assert exported["input.value.length"] == len(secret)
    assert exported["output.value.length"] == len(secret)
    assert exported["rag.model"] == "test"
    log = content_privacy_processor(None, "info", {"event": "condense", "original": secret, "condensed": secret, "error": secret})
    assert secret not in str(log)
    monkeypatch.setattr(privacy, "_include_content", True)
    assert tracing._safe_attributes({"input.value": secret})["input.value"] == secret


def test_agent_window_preserves_complete_tool_groups_and_unfinished_turn():
    messages = []
    for i in range(20):
        messages.extend([
            HumanMessage(content=f"q{i}", id=f"u{i}"),
            AIMessage(content="", id=f"a{i}", tool_calls=[{"name": "clock", "id": str(i), "args": {}}]),
            ToolMessage(content="time", id=f"t{i}", tool_call_id=str(i)),
        ])
    messages.append(HumanMessage(content="new", id="new"))
    messages.append(AIMessage(content="", id="pending", tool_calls=[{"name": "send", "id": "pending", "args": {}}]))
    removed = {m.id for m in context_removals(messages)}
    remaining = [m for m in messages if m.id not in removed]
    assert len([m for m in remaining if isinstance(m, HumanMessage)]) == 10
    assert remaining[0].type == "human" and remaining[-1].id == "pending"
    for i in range(20):
        assert (f"a{i}" in removed) == (f"t{i}" in removed) == (f"u{i}" in removed)


@pytest.mark.asyncio
async def test_usage_only_stream_chunk_is_metered_and_stream_closed():
    class Stream:
        close = AsyncMock()
        def __aiter__(self):
            return self.tokens()
        async def tokens(self):
            yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3, total_tokens=15))
    stream = Stream()
    usage = []
    result = await ModelAdapter().create(AsyncMock(return_value=stream), stream=True, usage_callback=usage.append)
    _ = [x async for x in result]
    assert usage == [{"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}]
    stream.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_deadline_cancels_producer_without_content_leak():
    from app.observability.streaming import with_heartbeat
    closed = asyncio.Event()
    async def source():
        try:
            await asyncio.sleep(60)
            yield "unvalidated"
        finally:
            closed.set()
    events = [v async for v in with_heartbeat(source(), heartbeat="progress", timeout_event="timeout", deadline=.04, interval=.01)]
    assert events[0] == "progress" and events[-1] == "timeout"
    assert "unvalidated" not in events and closed.is_set()


def test_docx_unpacked_limit_before_document_parser(monkeypatch):
    from app.chat import media
    monkeypatch.setattr(media, "MAX_DOCX_UNPACKED_BYTES", 100)
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 200)
    with pytest.raises(ValueError, match="Распакованный"):
        media.extract_docx_text(buf.getvalue())


@pytest.mark.asyncio
async def test_real_docx_worker_keeps_event_loop_responsive():
    from docx import Document
    from app.chat.media import parse_document
    buf = BytesIO()
    doc = Document()
    doc.add_paragraph("sample code")
    doc.save(buf)
    ticks = 0
    task = asyncio.create_task(parse_document(buf.getvalue(), "docx"))
    while not task.done():
        ticks += 1
        await asyncio.sleep(.005)
    assert await task == "sample code"
    assert ticks > 1


@pytest.mark.asyncio
async def test_parser_timeout_kills_and_reaps_worker(monkeypatch):
    from app.chat import media
    monkeypatch.setattr(media, "PARSE_TIMEOUT", .01)
    process = SimpleNamespace(returncode=None, kill=lambda: None, wait=AsyncMock())
    killed = []
    process.kill = lambda: killed.append(True)
    async def communicate(data):
        await asyncio.sleep(60)
    process.communicate = communicate
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(ValueError, match="времени"):
        await media.parse_document(b"data", "pdf")
    assert killed == [True]
    process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalation_survives_reopen_and_is_owner_scoped(tmp_path):
    from fastapi import FastAPI, Request
    from httpx import AsyncClient, ASGITransport
    from app.routers.escalations import router
    app = FastAPI()
    app.state.settings = SimpleNamespace(rag_pipeline_storage_dir=tmp_path)
    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.owner_id = request.headers.get("X-User-ID", "1001")
        return await call_next(request)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ticket = (await client.post("/escalations", json={"question": "Review this code"})).json()
        assert ticket["status"] == "open" and len(ticket["id"]) == 32
        path = f"/escalations/{ticket['id']}"
        assert (await client.get(path)).json() == ticket
        assert (await client.get(path, headers={"X-User-ID": "2002"})).status_code == 404
        assert (await client.delete(path, headers={"X-User-ID": "2002"})).status_code == 404
        assert (await client.delete(path)).json()["status"] == "cancelled"
        assert (await client.get(path)).json()["status"] == "cancelled"
