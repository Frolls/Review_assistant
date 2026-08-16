from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, model_validator

router = APIRouter(prefix="/agent", tags=["agent"])


class AgentStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=200)
    input: dict[str, Any] | None = None
    resume: bool | None = None
    user_role: Literal["read-only", "write-with-approve", "full"] = "write-with-approve"

    @model_validator(mode="after")
    def exactly_one_operation(self) -> AgentStreamRequest:
        if (self.input is None) == (self.resume is None):
            raise ValueError("provide exactly one of input or resume")
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

    config = {
        "configurable": {
            "thread_id": payload.thread_id,
            "user_role": payload.user_role,
        }
    }
    graph_input: dict[str, Any] | Command
    if payload.resume is not None:
        graph_input = Command(resume=payload.resume)
    else:
        graph_input = payload.input or {}

    async def event_stream() -> AsyncIterator[str]:
        yield _sse("start", {"thread_id": payload.thread_id})
        try:
            async for stream_type, chunk in graph.astream(
                graph_input,
                config,
                stream_mode=["updates", "messages"],
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
                {"type": type(exc).__name__, "message": str(exc)},
            )

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

    config = {"configurable": {"thread_id": payload.thread_id}}
    initial = {
        "messages": [],
        "question": payload.question,
        "research": "",
        "final_answer": "",
        "handoff_count": 0,
    }
    await graph.ainvoke(initial, config=config)
    snapshot = await graph.aget_state(config)
    values = snapshot.values
    return {
        "answer": values.get("final_answer", ""),
        "handoff_count": values.get("handoff_count", 0),
        "thread_id": payload.thread_id,
    }
