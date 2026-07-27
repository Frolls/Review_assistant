from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.chat.deps import get_repository
from app.chat.repository import ChatRepository


router = APIRouter(prefix="/chats", tags=["chat-feedback"])


class FeedbackIn(BaseModel):
    value: Literal["up", "down"]
    sources: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/{chat_id}/messages/{message_id}/feedback")
async def save_feedback(
    chat_id: UUID,
    message_id: UUID,
    payload: FeedbackIn,
    repository: ChatRepository = Depends(get_repository),
) -> dict[str, str]:
    try:
        await repository.save_feedback(
            chat_id,
            message_id,
            payload.value,
            sources=payload.sources,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Message not found") from exc
    return {"status": "ok"}
