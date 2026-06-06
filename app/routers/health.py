from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.schemas.chat import ErrorResponse


router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Check service liveness",
    responses={
        200: {"model": HealthResponse, "description": "Service is alive."},
        422: {"model": ErrorResponse, "description": "Request validation error."},
        429: {"model": ErrorResponse, "description": "LLM provider rate limit."},
        502: {"model": ErrorResponse, "description": "LLM provider authentication or upstream error."},
        504: {"model": ErrorResponse, "description": "LLM provider timeout."},
    },
)
async def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok")
