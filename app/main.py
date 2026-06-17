from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from redis.asyncio import Redis
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.config import get_settings
from app.core.exceptions import (
    LLMAuthError,
    LLMEmptyResponseError,
    LLMError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.observability.logging import get_logger, setup_logging
from app.observability.tracing import setup_tracing
from app.routers.chat import router as chat_router
from app.routers.health import router as health_router
from app.routers.models import router as models_router


logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    setup_tracing()
    openai_client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url,
        timeout=settings.request_timeout,
    )
    cache = Redis.from_url(settings.redis_url)
    app.state.openai = openai_client
    app.state.cache = cache
    app.state.llm_semaphore = asyncio.Semaphore(settings.max_concurrency)
    try:
        yield
    finally:
        await openai_client.close()
        await cache.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(
        title="LLM HTTP Service",
        version="1.0.0",
        description=(
            "FastAPI service with chat completion, streaming, and Redis cache. "
            "Use OPENAI_BASE_URL to point either to OpenAI directly or to a LiteLLM proxy."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.middleware("http")(request_context_middleware)
    app.add_exception_handler(LLMError, llm_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(health_router)
    app.include_router(models_router)
    app.include_router(chat_router)
    return app


async def request_context_middleware(request: Request, call_next):
    clear_contextvars()
    request_id = request.headers.get("X-Request-ID") or uuid4().hex[:12]
    user_id = request.headers.get("X-User-ID")
    request.state.request_id = request_id
    started_at = time.perf_counter()

    bind_contextvars(
        request_id=request_id,
        user_id=user_id,
        path=request.url.path,
        method=request.method,
    )

    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http_request_completed",
            status=response.status_code,
            latency_ms=duration_ms,
        )
        return response
    finally:
        clear_contextvars()


async def llm_error_handler(_: Request, exc: LLMError) -> JSONResponse:
    if isinstance(exc, LLMRateLimitError):
        status_code = 429
    elif isinstance(exc, LLMQuotaError):
        status_code = 429
    elif isinstance(exc, LLMTimeoutError):
        status_code = 504
    elif isinstance(exc, LLMAuthError):
        status_code = 502
    elif isinstance(exc, LLMEmptyResponseError):
        status_code = 502
    else:
        status_code = 502

    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


async def request_validation_error_handler(
    _: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    details = [
        {
            "field": ".".join(str(item) for item in error["loc"] if item != "body"),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed.",
                "details": details,
            }
        },
    )


app = create_app()
