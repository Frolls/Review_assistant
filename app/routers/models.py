from __future__ import annotations

from fastapi import APIRouter

from app.schemas.chat import ErrorResponse
from app.schemas.models import MODELS_CATALOG, ModelCard


router = APIRouter(tags=["models"])


@router.get(
    "/models",
    response_model=list[ModelCard],
    summary="List supported OpenAI models and prices",
    responses={
        200: {"model": list[ModelCard], "description": "Static OpenAI model catalog with prices."},
        422: {"model": ErrorResponse, "description": "Request validation error."},
        429: {"model": ErrorResponse, "description": "LLM provider rate limit."},
        502: {"model": ErrorResponse, "description": "LLM provider authentication or upstream error."},
        504: {"model": ErrorResponse, "description": "LLM provider timeout."},
    },
)
async def list_models() -> list[ModelCard]:
    return MODELS_CATALOG
