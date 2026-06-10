from __future__ import annotations

from typing import Final

from app.schemas.chat import ChatResponse, ErrorResponse
from app.schemas.health import HealthResponse, ReadinessResponse
from app.schemas.models import ModelCard


API_ERROR_RESPONSES: Final = {
    422: {"model": ErrorResponse, "description": "Request validation error."},
    429: {"model": ErrorResponse, "description": "LLM provider rate limit."},
    502: {"model": ErrorResponse, "description": "LLM provider authentication or upstream error."},
    504: {"model": ErrorResponse, "description": "LLM provider timeout."},
}

CHAT_RESPONSES: Final = {
    200: {"model": ChatResponse, "description": "Chat completion generated successfully."},
    **API_ERROR_RESPONSES,
}

CHAT_STREAM_RESPONSES: Final = {
    200: {"description": "Server-sent event stream with content chunks and final usage."},
    **API_ERROR_RESPONSES,
}

MODELS_RESPONSES: Final = {
    200: {"model": list[ModelCard], "description": "Static OpenAI model catalog with prices."},
    **API_ERROR_RESPONSES,
}

HEALTH_RESPONSES: Final = {
    200: {"model": HealthResponse, "description": "Service process is alive."},
}

READINESS_RESPONSES: Final = {
    200: {"model": ReadinessResponse, "description": "Service is ready to accept traffic."},
    503: {"model": ReadinessResponse, "description": "Service is running but Redis is unavailable."},
}
