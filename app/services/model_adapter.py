"""Transport policy shared by chat, retrieval and the persistent agent.

Retries only model requests, never tool effects or partially consumed streams.
Caching remains a caller policy; agent calls are deliberately uncached.
"""
import asyncio
import inspect
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, AuthenticationError, RateLimitError

from app.core.exceptions import LLMAuthError, LLMError, LLMRateLimitError, LLMTimeoutError, LLMQuotaError
from app.observability.logging import get_logger

PROMPT_VERSION = "review-2026-11-03-v12"
logger = get_logger(__name__)


def map_error(exc: Exception) -> LLMError:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or type(exc).__name__ == "APITimeoutError":
        return LLMTimeoutError()
    if getattr(exc, "code", None) == "insufficient_quota" or "insufficient_quota" in str(getattr(exc, "body", "")):
        return LLMQuotaError()
    if isinstance(exc, AuthenticationError):
        return LLMAuthError()
    if isinstance(exc, RateLimitError) or getattr(exc, "status_code", None) == 429:
        return LLMRateLimitError()
    return LLMError()


def usage_of(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is not None:
        return {key: int(getattr(usage, key, 0) or 0) for key in (
            "prompt_tokens", "completion_tokens", "total_tokens")}
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        return {"prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0))}
    return None


class ModelAdapter:
    def __init__(self, timeout: float = 120):
        self.timeout = timeout

    async def create(self, call, *, usage_callback=None, **kwargs):
        started = time.monotonic()
        deadline = started + self.timeout
        try:
            async with asyncio.timeout_at(deadline):
                for attempt in range(2):
                    try:
                        result = await call(**kwargs)
                        break
                    except Exception as exc:
                        retryable = isinstance(exc, APIConnectionError) or getattr(exc, "status_code", 0) in {429, 500, 502, 503, 504}
                        if getattr(exc, "status_code", None) == 429:
                            retryable = not isinstance(map_error(exc), LLMQuotaError)
                        if not retryable or attempt:
                            raise
                        await asyncio.sleep(0.2)
        except Exception as exc:
            if not isinstance(exc, (APIConnectionError, APIStatusError, TimeoutError)) and getattr(exc, "status_code", None) != 429:
                raise
            self._record(started, kwargs, None, type(exc).__name__)
            raise map_error(exc) from exc
        if kwargs.get("stream"):
            return self._stream(result, started, deadline, kwargs, usage_callback)
        usage = usage_of(result)
        if usage_callback is not None and usage is not None:
            usage_callback(usage)
        self._record(started, kwargs, usage)
        return result

    async def invoke(self, model, messages):
        return await self.create(lambda **_: model.ainvoke(messages), model=type(model).__name__)

    async def _stream(self, stream, started, deadline, kwargs, callback):
        usage = None
        error = None
        try:
            async with asyncio.timeout_at(deadline):
                async for chunk in stream:
                    value = usage_of(chunk)
                    if value is not None:
                        usage = value
                    yield chunk
        except (APIConnectionError, APIStatusError, TimeoutError) as exc:
            error = type(exc).__name__
            raise map_error(exc) from exc
        finally:
            if callback is not None and usage is not None:
                callback(usage)
            self._record(started, kwargs, usage, error)
            close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    def _record(self, started, kwargs, usage, error=None):
        logger.info("model.call", model=kwargs.get("model", "unknown"),
                    prompt_version=PROMPT_VERSION,
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    usage_available=usage is not None, usage=usage or {}, error_type=error)
