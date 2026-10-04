from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.deps.providers import CacheDep
from app.routers.responses import HEALTH_RESPONSES, READINESS_RESPONSES
from app.schemas.health import HealthResponse, ReadinessResponse


router = APIRouter(tags=["health"])
REDIS_READY_TIMEOUT_SECONDS = 1.5


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Check service liveness",
    responses=HEALTH_RESPONSES,
)
async def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Check service readiness",
    responses=READINESS_RESPONSES,
)
async def readiness_check(request: Request, cache: CacheDep) -> ReadinessResponse | JSONResponse:
    state = request.app.state
    checks = {}

    async def probe(name, operation):
        try:
            await asyncio.wait_for(operation(), timeout=3)
            checks[name] = "up"
        except Exception:
            checks[name] = "down"

    async def database():
        from sqlalchemy import text
        async with state.db_sessionmaker() as session:
            await session.execute(text("SELECT 1"))

    async def vector_store():
        await state.vector_store.client.get_collection(state.settings.rag_collection)

    probes = [probe("redis", cache.ping), probe("qdrant", vector_store),
              probe("model_api", state.openai.models.list)]
    if state.settings.chat_repository == "postgres":
        probes.append(probe("postgres", database))
    await asyncio.gather(*probes)
    checks["rag"] = "up" if getattr(state, "rag_service", None) is not None else "down"
    checks["agent"] = "up" if getattr(state, "agent_graph", None) is not None else "down"
    ok = all(value == "up" for value in checks.values())
    result = ReadinessResponse(status="ok" if ok else "degraded", redis=checks["redis"], dependencies=checks)
    if not ok:
        return JSONResponse(status_code=503, content=result.model_dump())
    return result
