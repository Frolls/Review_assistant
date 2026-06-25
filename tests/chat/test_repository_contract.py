from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from app.chat.domain import ChatMessage
from app.chat.repositories.json_repo import JsonChatRepository


@pytest_asyncio.fixture(params=["json", "postgres"])
async def repository(request: pytest.FixtureRequest, tmp_path):
    if request.param == "json":
        yield JsonChatRepository(base_dir=tmp_path)
        return

    pytest.importorskip("sqlalchemy")
    pytest.importorskip("asyncpg")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.chat.repositories.pg_models import Base
    from app.chat.repositories.pg_repo import PostgresChatRepository

    async with _postgres_database_url() as database_url:
        engine = create_async_engine(database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            yield PostgresChatRepository(session=session)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_chat_and_read_back(repository):
    chat = await repository.create_chat(
        owner_external_id="telegram-42",
        interface="telegram",
        system_prompt="Be concise.",
    )

    loaded = await repository.get_chat(chat.id)

    assert loaded == chat


@pytest.mark.asyncio
async def test_append_message_and_list_messages_are_chronological(repository):
    chat = await repository.create_chat("owner-1", "cli")
    first = _message(chat.id, "user", "one", seconds=1)
    second = _message(chat.id, "assistant", "two", seconds=2)

    await repository.append_message(chat.id, second)
    await repository.append_message(chat.id, first)

    messages = await repository.list_messages(chat.id)

    assert [message.content for message in messages] == ["one", "two"]


@pytest.mark.asyncio
async def test_list_messages_limit_returns_last_messages(repository):
    chat = await repository.create_chat("owner-1", "cli")
    for index in range(5):
        await repository.append_message(
            chat.id,
            _message(chat.id, "user", f"message-{index}", seconds=index),
        )

    messages = await repository.list_messages(chat.id, limit=2)

    assert [message.content for message in messages] == ["message-3", "message-4"]


@pytest.mark.asyncio
async def test_soft_delete_hides_old_messages_but_new_messages_are_visible(repository):
    chat = await repository.create_chat("owner-1", "cli")
    await repository.append_message(chat.id, _message(chat.id, "user", "before", seconds=1))

    await repository.soft_delete_messages(chat.id)

    assert await repository.list_messages(chat.id) == []

    await repository.append_message(chat.id, _message(chat.id, "user", "after", seconds=2))

    messages = await repository.list_messages(chat.id)
    assert [message.content for message in messages] == ["after"]


@pytest.mark.asyncio
async def test_get_chat_unknown_uuid_returns_none(repository):
    assert await repository.get_chat(uuid4()) is None


def _message(chat_id, role: str, content: str, *, seconds: int) -> ChatMessage:
    return ChatMessage(
        chat_id=chat_id,
        role=role,
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds),
    )


@asynccontextmanager
async def _postgres_database_url() -> AsyncIterator[str]:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url:
        yield database_url
        return

    if not os.getenv("DOCKER_HOST") and not Path("/var/run/docker.sock").exists():
        pytest.skip("TEST_DATABASE_URL or Docker is required for Postgres repository tests")

    testcontainers = pytest.importorskip("testcontainers.postgres")
    try:
        with testcontainers.PostgresContainer("postgres:16-alpine") as postgres:
            yield postgres.get_connection_url(driver="asyncpg")
    except Exception as exc:
        pytest.skip(f"Postgres test container is unavailable: {exc}")
