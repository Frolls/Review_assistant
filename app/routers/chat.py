from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.deps.providers import LLMServiceDep
from app.routers.responses import CHAT_RESPONSES, CHAT_STREAM_RESPONSES
from app.schemas.chat import ChatRequest, ChatResponse


router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Create a chat completion",
    responses=CHAT_RESPONSES,
)
async def chat(payload: ChatRequest, llm_service: LLMServiceDep) -> ChatResponse:
    return await llm_service.complete(payload)


@router.post(
    "/chat/stream",
    summary="Stream a chat completion",
    responses=CHAT_STREAM_RESPONSES,
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
