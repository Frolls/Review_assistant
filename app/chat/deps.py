from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from openai import AsyncOpenAI

from app.chat.repositories.json_repo import JsonChatRepository
from app.chat.repository import ChatRepository
from app.chat.service import ChatService


async def get_llm_client(request: Request) -> AsyncOpenAI:
    return request.app.state.openai


async def get_repository(
    request: Request,
) -> AsyncIterator[ChatRepository]:
    settings = request.app.state.settings
    if settings.chat_repository == "json":
        yield JsonChatRepository(base_dir=settings.chat_storage_dir)
        return

    if settings.chat_repository == "postgres":
        from app.chat.repositories.pg_repo import PostgresChatRepository

        sessionmaker = getattr(request.app.state, "db_sessionmaker", None)
        if sessionmaker is None:
            raise RuntimeError("Postgres chat repository requested, but database is not configured")
        async with sessionmaker() as session:
            yield PostgresChatRepository(session=session)
        return

    raise ValueError(
        "Unsupported CHAT_REPOSITORY value. Expected one of: json, postgres."
    )


async def get_chat_service(
    request: Request,
    repo: Annotated[ChatRepository, Depends(get_repository)],
    llm: Annotated[AsyncOpenAI, Depends(get_llm_client)],
) -> ChatService:
    settings = request.app.state.settings
    return ChatService(
        repository=repo,
        llm_client=llm,
        model=settings.default_model,
        context_strategy=settings.chat_context_strategy,
        keep_recent=settings.chat_context_window,
    )


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
