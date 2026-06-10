from __future__ import annotations

from fastapi import APIRouter

from app.routers.responses import MODELS_RESPONSES
from app.schemas.models import MODELS_CATALOG, ModelCard


router = APIRouter(tags=["models"])


@router.get(
    "/models",
    response_model=list[ModelCard],
    summary="List supported OpenAI models and prices",
    responses=MODELS_RESPONSES,
)
async def list_models() -> list[ModelCard]:
    return MODELS_CATALOG
