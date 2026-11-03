"""Durable, owner-scoped review requests. Registration does not send messages."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/escalations", tags=["escalations"])


class EscalationIn(BaseModel):
    question: str = Field(min_length=1, max_length=12000)


@asynccontextmanager
async def database(request: Request):
    path = request.app.state.settings.rag_pipeline_storage_dir / "escalations.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("CREATE TABLE IF NOT EXISTS escalations (id TEXT PRIMARY KEY, owner TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)")
        yield db


@router.post("", status_code=201)
async def create_escalation(payload: EscalationIn, request: Request):
    ticket = {"id": uuid4().hex, "question": payload.question, "status": "open", "created_at": datetime.now(UTC).isoformat()}
    async with database(request) as db:
        await db.execute("INSERT INTO escalations VALUES (?, ?, ?, ?, ?)",
                         (ticket["id"], request.state.owner_id, ticket["question"], ticket["status"], ticket["created_at"]))
        await db.commit()
    return ticket


@router.get("/{ticket_id}")
async def get_escalation(ticket_id: str, request: Request):
    async with database(request) as db:
        async with db.execute("SELECT id, question, status, created_at FROM escalations WHERE id=? AND owner=?", (ticket_id, request.state.owner_id)) as cursor:
            row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return dict(row)


@router.delete("/{ticket_id}")
async def cancel_escalation(ticket_id: str, request: Request):
    async with database(request) as db:
        cursor = await db.execute("UPDATE escalations SET status='cancelled' WHERE id=? AND owner=?", (ticket_id, request.state.owner_id))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        await db.commit()
    return await get_escalation(ticket_id, request)
