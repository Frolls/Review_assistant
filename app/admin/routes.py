from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.admin.auth import require_admin
from app.chat.repositories.pg_models import (
    BroadcastQueueRow,
    ChatMessageRow,
    ChatRow,
    MessageFeedbackRow,
    ModerationIncidentRow,
)


router = APIRouter(
    prefix="/chats/admin",
    tags=["chat-admin"],
    dependencies=[Depends(require_admin)],
)


class TopQuestionOut(BaseModel):
    question: str
    count: int


class StatsOut(BaseModel):
    total_messages: int
    active_users: int
    avg_latency_ms: float
    moderation_block_rate: float
    feedback_up_ratio: float
    top_questions: list[TopQuestionOut] = Field(default_factory=list)


class UserOut(BaseModel):
    owner_external_id: str
    chat_count: int
    last_seen_at: datetime


class BroadcastIn(BaseModel):
    message: str
    interface_filter: Literal["telegram"]


class BroadcastOut(BaseModel):
    id: UUID
    status: str


class PendingBroadcastOut(BaseModel):
    id: UUID
    message: str
    interface: str
    owner_external_ids: list[str]


class CompleteBroadcastIn(BaseModel):
    status: Literal["sent", "failed"] = "sent"
    error: str | None = None


@router.get("/stats", response_model=StatsOut)
async def stats(request: Request, top_n: int = 5) -> StatsOut:
    sessionmaker = _sessionmaker(request)
    since = datetime.now(UTC) - timedelta(hours=24)
    async with sessionmaker() as session:
        total_messages = await session.scalar(
            select(func.count()).select_from(ChatMessageRow).where(ChatMessageRow.created_at >= since)
        )
        active_users = await session.scalar(
            select(func.count(func.distinct(ChatRow.owner_external_id)))
            .join(ChatMessageRow, ChatMessageRow.chat_id == ChatRow.id)
            .where(ChatMessageRow.created_at >= since)
        )
        avg_latency = await session.scalar(
            select(func.avg(ChatMessageRow.latency_ms)).where(
                ChatMessageRow.created_at >= since,
                ChatMessageRow.role == "assistant",
                ChatMessageRow.latency_ms.is_not(None),
            )
        )
        blocked = await session.scalar(
            select(func.count()).select_from(ModerationIncidentRow).where(
                ModerationIncidentRow.created_at >= since
            )
        )
        feedback_total = await session.scalar(
            select(func.count()).select_from(MessageFeedbackRow).where(
                MessageFeedbackRow.created_at >= since
            )
        )
        feedback_up = await session.scalar(
            select(func.count()).select_from(MessageFeedbackRow).where(
                MessageFeedbackRow.created_at >= since,
                MessageFeedbackRow.value == "up",
            )
        )

        normalized_question = func.lower(
            func.regexp_replace(ChatMessageRow.content, r"\s+", " ", "g")
        )
        top_rows = await session.execute(
            select(normalized_question.label("question"), func.count().label("count"))
            .where(
                ChatMessageRow.created_at >= since,
                ChatMessageRow.role == "user",
                ChatMessageRow.deleted_at.is_(None),
            )
            .group_by(normalized_question)
            .order_by(sa.desc("count"))
            .limit(max(0, min(top_n, 20)))
        )

    total = int(total_messages or 0)
    blocked_count = int(blocked or 0)
    moderation_denominator = total + blocked_count
    return StatsOut(
        total_messages=total,
        active_users=int(active_users or 0),
        avg_latency_ms=float(round(avg_latency or 0, 2)),
        moderation_block_rate=_ratio(blocked_count, moderation_denominator),
        feedback_up_ratio=_ratio(int(feedback_up or 0), int(feedback_total or 0)),
        top_questions=[
            TopQuestionOut(question=str(row.question), count=int(row.count))
            for row in top_rows
        ],
    )


@router.get("/users", response_model=list[UserOut])
async def users(request: Request, limit: int = 50) -> list[UserOut]:
    sessionmaker = _sessionmaker(request)
    async with sessionmaker() as session:
        rows = await session.execute(
            select(
                ChatRow.owner_external_id,
                func.count(func.distinct(ChatRow.id)).label("chat_count"),
                func.max(func.coalesce(ChatMessageRow.created_at, ChatRow.created_at)).label(
                    "last_seen_at"
                ),
            )
            .outerjoin(ChatMessageRow, ChatMessageRow.chat_id == ChatRow.id)
            .group_by(ChatRow.owner_external_id)
            .order_by(sa.desc("last_seen_at"))
            .limit(max(1, min(limit, 200)))
        )
    return [
        UserOut(
            owner_external_id=str(row.owner_external_id),
            chat_count=int(row.chat_count),
            last_seen_at=row.last_seen_at,
        )
        for row in rows
    ]


@router.post("/broadcast", response_model=BroadcastOut)
async def broadcast(request: Request, payload: BroadcastIn) -> BroadcastOut:
    sessionmaker = _sessionmaker(request)
    async with sessionmaker() as session:
        row = BroadcastQueueRow(
            message=payload.message,
            interface=payload.interface_filter,
            status="pending",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return BroadcastOut(id=row.id, status=row.status)


@router.get("/broadcast/pending", response_model=list[PendingBroadcastOut])
async def pending_broadcasts(
    request: Request,
    interface_filter: Literal["telegram"] = "telegram",
    limit: int = 5,
) -> list[PendingBroadcastOut]:
    sessionmaker = _sessionmaker(request)
    async with sessionmaker() as session:
        result = await session.execute(
            select(BroadcastQueueRow)
            .where(
                BroadcastQueueRow.status == "pending",
                BroadcastQueueRow.interface == interface_filter,
            )
            .order_by(BroadcastQueueRow.created_at.asc())
            .limit(max(1, min(limit, 20)))
        )
        broadcasts = list(result.scalars())
        if not broadcasts:
            return []

        recipients = await session.scalars(
            select(ChatRow.owner_external_id)
            .where(ChatRow.interface == interface_filter)
            .group_by(ChatRow.owner_external_id)
        )
        owner_external_ids = [str(item) for item in recipients]
        broadcast_ids = [item.id for item in broadcasts]
        await session.execute(
            update(BroadcastQueueRow)
            .where(BroadcastQueueRow.id.in_(broadcast_ids))
            .values(status="processing", updated_at=datetime.now(UTC))
        )
        await session.commit()

    return [
        PendingBroadcastOut(
            id=item.id,
            message=item.message,
            interface=item.interface,
            owner_external_ids=owner_external_ids,
        )
        for item in broadcasts
    ]


@router.post("/broadcast/{broadcast_id}/complete")
async def complete_broadcast(
    request: Request,
    broadcast_id: UUID,
    payload: CompleteBroadcastIn,
) -> dict[str, str]:
    sessionmaker = _sessionmaker(request)
    async with sessionmaker() as session:
        await session.execute(
            update(BroadcastQueueRow)
            .where(BroadcastQueueRow.id == broadcast_id)
            .values(
                status=payload.status,
                error=payload.error,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return {"status": "ok"}


def _sessionmaker(request: Request):
    sessionmaker = getattr(request.app.state, "db_sessionmaker", None)
    if sessionmaker is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "postgres_required", "message": "Admin endpoints require Postgres."},
        )
    return sessionmaker


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)
