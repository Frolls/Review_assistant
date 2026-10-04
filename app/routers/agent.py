from __future__ import annotations

import dataclasses
import asyncio
from hashlib import sha256
from weakref import WeakValueDictionary
from uuid import uuid4
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, model_validator

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

def _thread_lock(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock

def _owned_thread(request: Request, thread: str) -> str:
    owner = request.state.owner_id
    return sha256(f"{owner}:{thread}".encode()).hexdigest()


router = APIRouter(prefix="/agent", tags=["agent"])


class AgentStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=200)
    input: dict[str, Any] | None = None
    resume: bool | None = None
    request_id: str | None = None
    user_role: Literal["read-only", "write-with-approve"] = "write-with-approve"

    @model_validator(mode="after")
    def exactly_one_operation(self) -> AgentStreamRequest:
        if (self.input is None) == (self.resume is None):
            raise ValueError("provide exactly one of input or resume")
        if self.resume is not None and not self.request_id:
            raise ValueError("request_id of the pending action is required")
        if self.input is not None:
            messages = self.input.get("messages", [])
            if set(self.input) != {"messages"} or not isinstance(messages, list) or len(messages) != 1:
                raise ValueError("Only one new user message is accepted")
            msg = messages[0]
            if not isinstance(msg, dict) or set(msg) != {"role", "content"} or msg["role"] != "user":
                raise ValueError("Only user role and text content are accepted")
            if not isinstance(msg["content"], str) or not 1 <= len(msg["content"]) <= 20000:
                raise ValueError("User content must contain 1..20000 characters")
        return self


class AgentReviewRequest(BaseModel):
    """Input for the production researcher → writer supervisor."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=20_000)
    thread_id: str = Field(default="agent-review", min_length=1, max_length=200)


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if hasattr(value, "_asdict"):
        return value._asdict()
    if hasattr(value, "value"):
        return {"value": value.value}
    if isinstance(value, (set, frozenset)):
        return list(value)
    return str(value)


def _sse(event: str, payload: Any) -> str:
    body = json.dumps(
        {"event": event, "data": payload},
        ensure_ascii=False,
        default=_json_default,
        separators=(",", ":"),
    )
    return f"data: {body}\n\n"


def _contains_interrupt(payload: Any) -> bool:
    if isinstance(payload, dict):
        return "__interrupt__" in payload or any(
            _contains_interrupt(value) for value in payload.values()
        )
    if isinstance(payload, (list, tuple)):
        return any(_contains_interrupt(value) for value in payload)
    return False


def _meaningful_message_chunk(payload: Any) -> bool:
    """Drop provider heartbeat/thinking chunks that contain no user-visible data."""

    message = payload[0] if isinstance(payload, (list, tuple)) and payload else payload
    if getattr(message, "content", None):
        return True
    if getattr(message, "tool_calls", None) or getattr(
        message, "tool_call_chunks", None
    ):
        return True
    if getattr(message, "usage_metadata", None):
        return True
    response_metadata = getattr(message, "response_metadata", {}) or {}
    return bool(response_metadata.get("finish_reason"))


def _compact_message_chunk(payload: Any) -> dict[str, Any]:
    """Expose token/tool deltas without repeating LangGraph metadata on every token."""

    if isinstance(payload, (list, tuple)) and payload:
        message = payload[0]
        metadata = (
            payload[1] if len(payload) > 1 and isinstance(payload[1], dict) else {}
        )
    else:
        message = payload
        metadata = {}
    response_metadata = getattr(message, "response_metadata", {}) or {}
    return {
        "content": getattr(message, "content", ""),
        "tool_calls": getattr(message, "tool_calls", None) or [],
        "tool_call_chunks": getattr(message, "tool_call_chunks", None) or [],
        "finish_reason": response_metadata.get("finish_reason"),
        "node": metadata.get("langgraph_node"),
    }


@router.post("/stream", summary="Stream a persistent LangGraph run over SSE")
async def stream_agent(
    payload: AgentStreamRequest, request: Request
) -> StreamingResponse:
    graph = getattr(request.app.state, "agent_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="persistent agent is not ready")

    owned_thread = _owned_thread(request, payload.thread_id)
    config = {
        "configurable": {
            "thread_id": owned_thread,
            "allowed_recipient": request.state.owner_id,
            "user_role": payload.user_role,
        }
    }
    graph_input: dict[str, Any] | Command
    if payload.resume is not None:
        graph_input = Command(resume=payload.resume)
    else:
        graph_input = payload.input or {}

    lock = _thread_lock(owned_thread)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Этот диалог уже обрабатывается")
    await lock.acquire()
    try:
        snapshot = await graph.aget_state(config)
        pending = snapshot.values.get("pending_action") if snapshot.values else None
        if payload.resume is not None:
            if not snapshot.next or not pending or pending.get("request_id") != payload.request_id:
                raise HTTPException(status_code=409, detail="Подтверждение устарело или действие уже завершено")
        elif snapshot.next:
            raise HTTPException(status_code=409, detail="Сначала подтвердите или отмените предыдущее действие")
        if payload.input:
            from app.moderation import ModerationService
            result = await ModerationService().check_input(payload.input["messages"][0]["content"])
            if not result.allowed:
                raise HTTPException(status_code=403, detail="Сообщение заблокировано модерацией")
    except BaseException:
        lock.release()
        raise

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield _sse("start", {"thread_id": payload.thread_id})
            async with request.app.state.llm_semaphore:
                async for stream_type, chunk in graph.astream(
                    graph_input,
                    config,
                    stream_mode=["updates"],
                ):
                    if stream_type == "messages" and not _meaningful_message_chunk(chunk):
                        continue
                    event = "interrupt" if _contains_interrupt(chunk) else stream_type
                    event_payload = (
                        _compact_message_chunk(chunk)
                        if stream_type == "messages"
                        else chunk
                    )
                    yield _sse(event, event_payload)

                snapshot = await graph.aget_state(config)
                interrupts = [
                    item
                    for task in snapshot.tasks
                    for item in getattr(task, "interrupts", ())
                ]
                if interrupts:
                    yield _sse(
                        "paused",
                        {"interrupts": interrupts, "next": snapshot.next},
                    )
                else:
                    yield _sse("done", {"next": snapshot.next})
        except Exception as exc:  # noqa: BLE001 - headers may already be sent
            yield _sse(
                "error",
                {"type": type(exc).__name__, "message": "Не удалось завершить запрос агента. Повторите позже."},
            )

        finally:
            lock.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/review", summary="Run the researcher → writer supervisor")
async def review_agent(payload: AgentReviewRequest, request: Request) -> dict[str, Any]:
    """Run the selected multi-agent graph for a grounded review question."""

    graph = getattr(request.app.state, "multi_agent_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="multi-agent supervisor is not ready")

    from app.moderation import ModerationService
    moderator = ModerationService()
    if not (await moderator.check_input(payload.question)).allowed:
        raise HTTPException(status_code=403, detail="Сообщение заблокировано модерацией")
    config = {"configurable": {"thread_id": _owned_thread(request, payload.thread_id) + ":" + uuid4().hex}}
    initial = {
        "messages": [],
        "question": payload.question,
        "research": "",
        "final_answer": "",
        "handoff_count": 0,
    }
    async with request.app.state.llm_semaphore:
        values = await graph.ainvoke(initial, config=config)
    answer = values.get("final_answer", "")
    if not (await moderator.check_output(answer)).allowed:
        answer = "Ответ заблокирован модерацией."
    return {
        "answer": answer,
        "handoff_count": values.get("handoff_count", 0),
        "thread_id": payload.thread_id,
    }


@router.get("/pending")
async def pending_action(request: Request, thread_id: str = "telegram-actions"):
    graph = getattr(request.app.state, "agent_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="Agent unavailable")
    snapshot = await graph.aget_state({"configurable": {"thread_id": _owned_thread(request, thread_id)}})
    return {"pending": snapshot.values.get("pending_action") if snapshot.next else None}
