from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.chat.deps import ChatServiceDep
from app.chat.domain import Chat, ChatMessage


router = APIRouter(prefix="/chats", tags=["stateful-chat"])


class CreateChatIn(BaseModel):
    owner_external_id: str
    interface: str
    system_prompt: str | None = None


class CreateChatOut(BaseModel):
    chat_id: UUID


class MessageIn(BaseModel):
    content: str


@router.post("", response_model=CreateChatOut)
async def create_chat(payload: CreateChatIn, chat_service: ChatServiceDep) -> CreateChatOut:
    chat = await chat_service.create_chat(
        owner_external_id=payload.owner_external_id,
        interface=payload.interface,
        system_prompt=payload.system_prompt,
    )
    return CreateChatOut(chat_id=chat.id)


@router.get("/{chat_id}", response_model=Chat)
async def get_chat(chat_id: UUID, chat_service: ChatServiceDep) -> Chat:
    chat = await chat_service.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@router.post("/{chat_id}/messages")
async def send_message(chat_id: UUID, payload: MessageIn, chat_service: ChatServiceDep) -> StreamingResponse:
    if await chat_service.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")

    async def event_stream() -> AsyncIterator[str]:
        async for chunk in chat_service.send_message(chat_id, payload.content):
            yield _format_sse_data(chunk)
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/{chat_id}/messages", response_model=list[ChatMessage])
async def list_messages(
    chat_id: UUID,
    chat_service: ChatServiceDep,
    limit: int = 50,
) -> list[ChatMessage]:
    return await chat_service.list_messages(chat_id, limit)


@router.delete("/{chat_id}/messages")
async def clear_messages(chat_id: UUID, chat_service: ChatServiceDep) -> dict[str, str]:
    await chat_service.clear_history(chat_id)
    return {"status": "ok"}


def _format_sse_data(chunk: str) -> str:
    lines = chunk.splitlines() or [chunk]
    return "".join(f"data: {line}\n" for line in lines) + "\n"
