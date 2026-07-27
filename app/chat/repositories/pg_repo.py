from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.domain import Chat, ChatMessage
from app.chat.repositories.pg_models import (
    ChatMessageRow,
    ChatRow,
    MessageFeedbackRow,
    ModerationIncidentRow,
)
from app.moderation import ModerationResult


class PostgresChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_chat(
        self,
        owner_external_id: str,
        interface: str,
        system_prompt: str | None = None,
    ) -> Chat:
        result = await self.session.execute(
            select(ChatRow)
            .where(
                ChatRow.owner_external_id == owner_external_id,
                ChatRow.interface == interface,
                ChatRow.system_prompt.is_(None)
                if system_prompt is None
                else ChatRow.system_prompt == system_prompt,
            )
            .order_by(ChatRow.created_at.asc())
            .limit(1)
        )
        existing_row = result.scalar_one_or_none()
        if existing_row is not None:
            return Chat.model_validate(existing_row, from_attributes=True)

        row = ChatRow(
            owner_external_id=owner_external_id,
            interface=interface,
            system_prompt=system_prompt,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return Chat.model_validate(row, from_attributes=True)

    async def get_chat(self, chat_id: UUID) -> Chat | None:
        row = await self.session.get(ChatRow, chat_id)
        if row is None:
            return None
        return Chat.model_validate(row, from_attributes=True)

    async def append_message(self, chat_id: UUID, message: ChatMessage) -> ChatMessage:
        if message.chat_id != chat_id:
            raise ValueError("message.chat_id must match chat_id")

        row = ChatMessageRow(
            id=message.id,
            chat_id=message.chat_id,
            role=message.role,
            content=message.content,
            tokens=message.tokens,
            latency_ms=message.latency_ms,
            media_refs=message.media_refs,
            created_at=message.created_at,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return ChatMessage.model_validate(row, from_attributes=True)

    async def list_messages(self, chat_id: UUID, limit: int = 50) -> list[ChatMessage]:
        if limit <= 0:
            return []

        result = await self.session.execute(
            select(ChatMessageRow)
            .where(ChatMessageRow.chat_id == chat_id, ChatMessageRow.deleted_at.is_(None))
            .order_by(ChatMessageRow.created_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars())
        return [
            ChatMessage.model_validate(row, from_attributes=True)
            for row in reversed(rows)
        ]

    async def soft_delete_messages(self, chat_id: UUID) -> None:
        await self.session.execute(
            update(ChatMessageRow)
            .where(ChatMessageRow.chat_id == chat_id, ChatMessageRow.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC))
        )
        await self.session.commit()

    async def record_moderation_incident(
        self,
        chat_id: UUID,
        direction: str,
        result: ModerationResult,
        text_hash: str,
        text_preview: str,
    ) -> None:
        self.session.add(
            ModerationIncidentRow(
                chat_id=chat_id,
                direction=direction,
                categories=result.categories,
                reasons=result.reasons,
                blocked_by=result.blocked_by,
                text_hash=text_hash,
                text_preview=text_preview,
            )
        )
        await self.session.commit()

    async def save_feedback(
        self,
        chat_id: UUID,
        message_id: UUID,
        value: str,
        *,
        sources: list[dict] | None = None,
    ) -> None:
        chat_result = await self.session.execute(
            select(ChatRow.owner_external_id)
            .join(ChatMessageRow, ChatMessageRow.chat_id == ChatRow.id)
            .where(ChatRow.id == chat_id, ChatMessageRow.id == message_id)
            .limit(1)
        )
        owner_external_id = chat_result.scalar_one_or_none()
        if owner_external_id is None:
            raise LookupError("Message was not found in this chat")

        statement = (
            insert(MessageFeedbackRow)
            .values(
                message_id=message_id,
                owner_external_id=owner_external_id,
                value=value,
                sources=sources or [],
            )
            .on_conflict_do_update(
                constraint="uq_message_feedback_owner_message",
                set_={
                    "value": value,
                    "sources": sources or [],
                    "created_at": datetime.now(UTC),
                },
            )
        )
        await self.session.execute(statement)
        await self.session.commit()
