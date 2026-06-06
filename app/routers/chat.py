from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.deps.providers import LLMServiceDep
from app.schemas.chat import ChatRequest, ChatResponse, ErrorResponse


router = APIRouter(tags=["chat"])

COMMON_RESPONSES = {
    422: {"model": ErrorResponse, "description": "Request validation error."},
    429: {"model": ErrorResponse, "description": "LLM provider rate limit."},
    502: {"model": ErrorResponse, "description": "LLM provider authentication or upstream error."},
    504: {"model": ErrorResponse, "description": "LLM provider timeout."},
}


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Create a chat completion",
    responses={
        200: {"model": ChatResponse, "description": "Chat completion generated successfully."},
        **COMMON_RESPONSES,
    },
)
async def chat(payload: ChatRequest, llm_service: LLMServiceDep) -> ChatResponse:
    return await llm_service.complete(payload)


@router.post(
    "/chat/stream",
    summary="Stream a chat completion",
    responses={
        200: {"description": "Server-sent event stream with content chunks and final usage."},
        **COMMON_RESPONSES,
    },
)
async def chat_stream(payload: ChatRequest, llm_service: LLMServiceDep) -> StreamingResponse:
    async def event_stream() -> AsyncIterator[str]:
        async for delta in llm_service.stream(payload):
            if delta.content is not None:
                yield f"data: {delta.content}\n\n"
                continue

            usage_payload = json.dumps(
                {"usage": delta.usage.model_dump(mode="json")},
                ensure_ascii=False,
            )
            yield f"data: {usage_payload}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
