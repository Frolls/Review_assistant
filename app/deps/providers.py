from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from openai import AsyncOpenAI
from redis.asyncio import Redis

from app.core.config import Settings, get_settings as load_settings
from app.services.llm import LLMService


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def get_openai(request: Request) -> AsyncOpenAI:
    return request.app.state.openai


def get_cache(request: Request) -> Redis:
    return request.app.state.cache


def get_llm_service(
    request: Request,
    openai: Annotated[AsyncOpenAI, Depends(get_openai)],
    cache: Annotated[Redis, Depends(get_cache)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LLMService:
    return LLMService(
        openai=openai,
        cache=cache,
        settings=settings,
        semaphore=getattr(request.app.state, "llm_semaphore", None),
        canary=getattr(request.app.state, "canary", ""),
    )


SettingsDep = Annotated[Settings, Depends(get_settings)]
CacheDep = Annotated[Redis, Depends(get_cache)]
LLMServiceDep = Annotated[LLMService, Depends(get_llm_service)]
