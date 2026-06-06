from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator

from openai import APITimeoutError, AsyncOpenAI, AuthenticationError, OpenAIError, RateLimitError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.exceptions import (
    LLMAuthError,
    LLMEmptyResponseError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.schemas.chat import ChatDelta, ChatRequest, ChatResponse, Usage


logger = logging.getLogger("llm-service")


class LLMService:
    def __init__(
        self,
        *,
        openai: AsyncOpenAI,
        cache: Redis,
        settings: Settings,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.openai = openai
        self.cache = cache
        self.settings = settings
        self._sem = semaphore or asyncio.Semaphore(settings.max_concurrency)

    async def complete(self, req: ChatRequest) -> ChatResponse:
        normalized_request = req.with_default_model(self.settings.default_model)
        cache_key = self._build_cache_key(normalized_request)
        started_at = time.perf_counter()

        cached_response = await self._read_cache(cache_key)
        if cached_response is not None:
            self._log_completion(
                model=normalized_request.model or self.settings.default_model,
                message_count=len(normalized_request.messages),
                status="cache_hit",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
            )
            return cached_response.model_copy(update={"cached": True})

        try:
            async with self._sem:
                async with asyncio.timeout(self.settings.request_timeout):
                    response = await self.openai.chat.completions.create(
                        model=normalized_request.model,
                        messages=[
                            message.model_dump(mode="json")
                            for message in normalized_request.messages
                        ],
                        temperature=normalized_request.temperature,
                        max_tokens=normalized_request.max_tokens,
                    )
        except TimeoutError as exc:
            raise LLMTimeoutError("LLM provider request timed out.") from exc
        except (RateLimitError, APITimeoutError, AuthenticationError, OpenAIError) as exc:
            self._log_completion(
                model=normalized_request.model or self.settings.default_model,
                message_count=len(normalized_request.messages),
                status="provider_error",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
            )
            raise self._translate_error(exc) from exc

        chat_response = ChatResponse.from_openai(response, cached=False)
        if not chat_response.content.strip():
            raise LLMEmptyResponseError()
        await self._write_cache(cache_key, chat_response)
        self._log_completion(
            model=chat_response.model,
            message_count=len(normalized_request.messages),
            status="ok",
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
        return chat_response

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        normalized_request = req.with_default_model(self.settings.default_model)
        started_at = time.perf_counter()
        first_token_ms: float | None = None

        async with self._sem:
            try:
                stream = await self.openai.chat.completions.create(
                    model=normalized_request.model,
                    messages=[
                        message.model_dump(mode="json")
                        for message in normalized_request.messages
                    ],
                    temperature=normalized_request.temperature,
                    max_tokens=normalized_request.max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                )
            except (RateLimitError, APITimeoutError, AuthenticationError, OpenAIError) as exc:
                raise self._translate_error(exc) from exc

            try:
                async for chunk in stream:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        yield ChatDelta(usage=Usage.from_openai(usage))

                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    content = self._extract_delta_text(delta)
                    if content:
                        if first_token_ms is None:
                            first_token_ms = round((time.perf_counter() - started_at) * 1000, 2)
                        yield ChatDelta(content=content)
            except (RateLimitError, APITimeoutError, AuthenticationError, OpenAIError) as exc:
                raise self._translate_error(exc) from exc
            finally:
                self._log_stream(
                    model=normalized_request.model or self.settings.default_model,
                    message_count=len(normalized_request.messages),
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                    ttft_ms=first_token_ms,
                )

    def _build_cache_key(self, req: ChatRequest) -> str:
        payload = req.model_dump(
            mode="json",
            exclude={"user_id", "session_id", "stream"},
        )
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return f"chat:{digest}"

    async def _read_cache(self, cache_key: str) -> ChatResponse | None:
        try:
            payload = await self.cache.get(cache_key)
        except RedisError:
            logger.warning("cache.read_failed", extra={"cache_key": cache_key}, exc_info=True)
            return None

        if payload is None:
            return None
        return ChatResponse.model_validate_json(payload)

    async def _write_cache(self, cache_key: str, response: ChatResponse) -> None:
        try:
            await self.cache.setex(
                cache_key,
                self.settings.cache_ttl_seconds,
                response.model_dump_json(),
            )
        except RedisError:
            logger.warning("cache.write_failed", extra={"cache_key": cache_key}, exc_info=True)

    def _translate_error(self, exc: Exception) -> LLMError:
        if isinstance(exc, RateLimitError):
            return LLMRateLimitError(str(exc))
        if isinstance(exc, APITimeoutError):
            return LLMTimeoutError(str(exc))
        if isinstance(exc, AuthenticationError):
            return LLMAuthError(str(exc))
        return LLMError(str(exc))

    def _extract_delta_text(self, delta: object) -> str:
        if delta is None:
            return ""
        for field_name in ("content", "reasoning", "reasoning_content", "text", "output_text"):
            text = self._coerce_text(getattr(delta, field_name, None))
            if text:
                return text
        return self._coerce_text(delta)

    def _log_completion(
        self,
        *,
        model: str,
        message_count: int,
        status: str,
        duration_ms: float,
    ) -> None:
        logger.info(
            "llm.complete model=%s messages=%s status=%s duration_ms=%.2f",
            model,
            message_count,
            status,
            duration_ms,
        )

    def _log_stream(
        self,
        *,
        model: str,
        message_count: int,
        duration_ms: float,
        ttft_ms: float | None,
    ) -> None:
        logger.info(
            "llm.stream model=%s messages=%s duration_ms=%.2f ttft_ms=%s",
            model,
            message_count,
            duration_ms,
            ttft_ms,
        )

    def _coerce_text(self, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(self._coerce_text(item) for item in value)
        if isinstance(value, dict):
            for field_name in ("text", "content", "reasoning", "output_text", "reasoning_content"):
                text = self._coerce_text(value.get(field_name))
                if text:
                    return text
            return ""
        for field_name in ("text", "content", "reasoning", "output_text", "reasoning_content"):
            if hasattr(value, field_name):
                text = self._coerce_text(getattr(value, field_name))
                if text:
                    return text
        return ""
