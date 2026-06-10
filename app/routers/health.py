from __future__ import annotations

import asyncio

from fastapi import APIRouter, status
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
async def readiness_check(cache: CacheDep) -> ReadinessResponse | JSONResponse:
    try:
        await asyncio.wait_for(cache.ping(), timeout=REDIS_READY_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.TimeoutError, RedisError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ReadinessResponse(status="degraded", redis="down").model_dump(),
        )

    return ReadinessResponse(status="ok", redis="up")
