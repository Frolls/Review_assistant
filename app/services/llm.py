from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator

from openai import APITimeoutError, AsyncOpenAI, AuthenticationError, OpenAIError, RateLimitError
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.exceptions import (
    LLMAuthError,
    LLMEmptyResponseError,
    LLMError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.observability.logging import get_logger
from app.observability.pii import prompt_hash, redact_pii_for_log
from app.schemas.chat import ChatDelta, ChatRequest, ChatResponse, Usage


logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


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
        with tracer.start_as_current_span("chat.request") as span:
            self._set_request_span_attributes(span, normalized_request)
            try:
                cached_response = await self._read_cache(cache_key)
                if cached_response is not None:
                    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                    cached_chat_response = cached_response.model_copy(update={"cached": True})
                    self._set_response_span_attributes(
                        span=span,
                        response=cached_chat_response,
                        duration_ms=duration_ms,
                        status="cache_hit",
                    )
                    await self._log_completion(
                        model=normalized_request.model or self.settings.default_model,
                        req=normalized_request,
                        response=cached_response,
                        message_count=len(normalized_request.messages),
                        status="cache_hit",
                        duration_ms=duration_ms,
                    )
                    return cached_chat_response

                try:
                    async with self._sem:
                        async with asyncio.timeout(self.settings.request_timeout):
                            response = await self._create_chat_completion_with_retry(
                                model=normalized_request.model,
                                messages=[
                                    message.model_dump(mode="json")
                                    for message in normalized_request.messages
                                ],
                                temperature=normalized_request.temperature,
                                max_tokens=normalized_request.max_tokens,
                            )
                except TimeoutError as exc:
                    translated = LLMTimeoutError("LLM provider request timed out.")
                    self._set_error_span_attributes(span, translated)
                    raise translated from exc
                except (RateLimitError, APITimeoutError, AuthenticationError, OpenAIError) as exc:
                    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                    await self._log_completion(
                        model=normalized_request.model or self.settings.default_model,
                        req=normalized_request,
                        response=None,
                        message_count=len(normalized_request.messages),
                        status="provider_error",
                        duration_ms=duration_ms,
                    )
                    translated = self._translate_error(exc)
                    self._set_error_span_attributes(span, translated)
                    raise translated from exc
                except Exception as exc:
                    if not self._is_rate_limit_error(exc):
                        raise
                    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                    await self._log_completion(
                        model=normalized_request.model or self.settings.default_model,
                        req=normalized_request,
                        response=None,
                        message_count=len(normalized_request.messages),
                        status="provider_error",
                        duration_ms=duration_ms,
                    )
                    translated = self._translate_error(exc)
                    self._set_error_span_attributes(span, translated)
                    raise translated from exc

                chat_response = ChatResponse.from_openai(response, cached=False)
                if not chat_response.content.strip():
                    error = LLMEmptyResponseError()
                    self._set_error_span_attributes(span, error)
                    raise error
                await self._write_cache(cache_key, chat_response)
                duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                self._set_response_span_attributes(
                    span=span,
                    response=chat_response,
                    duration_ms=duration_ms,
                    status="ok",
                )
                await self._log_completion(
                    model=chat_response.model,
                    req=normalized_request,
                    response=chat_response,
                    message_count=len(normalized_request.messages),
                    status="ok",
                    duration_ms=duration_ms,
                )
                return chat_response
            except LLMError:
                raise
            except Exception as exc:
                self._set_error_span_attributes(span, exc)
                raise

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
        if self._is_quota_error(exc):
            return LLMQuotaError(str(exc))
        if self._is_rate_limit_error(exc):
            return LLMRateLimitError(str(exc))
        if isinstance(exc, APITimeoutError):
            return LLMTimeoutError(str(exc))
        if isinstance(exc, AuthenticationError):
            return LLMAuthError(str(exc))
        return LLMError(str(exc))

    async def _create_chat_completion_with_retry(self, **kwargs):
        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                return await self.openai.chat.completions.create(**kwargs)
            except Exception as exc:
                if (
                    self._is_quota_error(exc)
                    or not self._is_rate_limit_error(exc)
                    or attempt == max_attempts - 1
                ):
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))

        raise LLMRateLimitError("LLM provider rate limit exceeded.")

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        return isinstance(exc, RateLimitError) or getattr(exc, "status_code", None) == 429

    def _is_quota_error(self, exc: Exception) -> bool:
        code = getattr(exc, "code", None)
        if code == "insufficient_quota":
            return True

        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict) and error.get("code") == "insufficient_quota":
                return True

        return "insufficient_quota" in str(exc)

    def _extract_delta_text(self, delta: object) -> str:
        if delta is None:
            return ""
        for field_name in ("content", "reasoning", "reasoning_content", "text", "output_text"):
            text = self._coerce_text(getattr(delta, field_name, None))
            if text:
                return text
        return self._coerce_text(delta)

    async def _log_completion(
        self,
        *,
        model: str,
        req: ChatRequest,
        response: ChatResponse | None,
        message_count: int,
        status: str,
        duration_ms: float,
    ) -> None:
        raw_prompt = self._prompt_text(req)
        prompt_preview = (await redact_pii_for_log(raw_prompt))[:120]
        usage = response.usage if response is not None else None
        logger.info(
            "llm_request_completed",
            model=model,
            input_tokens=usage.prompt_tokens if usage is not None else 0,
            output_tokens=usage.completion_tokens if usage is not None else 0,
            latency_ms=duration_ms,
            finish_reason=response.finish_reason if response is not None else None,
            status=status,
            message_count=message_count,
            prompt_hash=prompt_hash(raw_prompt),
            prompt_preview=prompt_preview,
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
            "llm_stream_completed",
            model=model,
            message_count=message_count,
            latency_ms=duration_ms,
            ttft_ms=ttft_ms,
        )

    def _prompt_text(self, req: ChatRequest) -> str:
        return "\n".join(f"{message.role}: {message.content}" for message in req.messages)

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

    def _set_request_span_attributes(self, span: trace.Span, req: ChatRequest) -> None:
        prompt_text = self._prompt_text(req)
        span.set_attribute("gen_ai.operation.name", "chat.completions")
        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.request.model", req.model or self.settings.default_model)
        span.set_attribute("input.value", prompt_text)

    def _set_response_span_attributes(
        self,
        *,
        span: trace.Span,
        response: ChatResponse,
        duration_ms: float,
        status: str,
    ) -> None:
        span.set_attribute("gen_ai.response.model", response.model)
        span.set_attribute("gen_ai.response.finish_reason", response.finish_reason or "")
        span.set_attribute("gen_ai.usage.input_tokens", response.usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", response.usage.completion_tokens)
        span.set_attribute("gen_ai.usage.total_tokens", response.usage.total_tokens)
        span.set_attribute("output.value", response.content)
        span.set_attribute("llm.cache_status", status)
        span.set_attribute("llm.latency_ms", duration_ms)
        span.set_status(Status(StatusCode.OK))

    def _set_error_span_attributes(self, span: trace.Span, exc: Exception) -> None:
        error_code = exc.code if isinstance(exc, LLMError) else exc.__class__.__name__
        error_message = exc.message if isinstance(exc, LLMError) else str(exc)
        span.set_attribute("error.type", error_code)
        span.set_attribute("error.message", error_message)
        span.set_status(Status(StatusCode.ERROR, error_message))
