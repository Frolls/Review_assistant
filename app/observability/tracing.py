from __future__ import annotations

import os
from contextlib import nullcontext, contextmanager
from typing import Any, ContextManager
from urllib.parse import urlparse, urlunparse


from app.observability.privacy import safe_attributes, configure_content_policy

_TRACING_CONFIGURED = False
_TRACER: Any | None = None


def setup_tracing(
    project_name: str | None = None,
    *,
    observability_include_content: bool | None = None,
) -> None:
    global _TRACER, _TRACING_CONFIGURED
    if observability_include_content is not None:
        configure_content_policy(observability_include_content)
    if os.environ.get("PHOENIX_TRACING_ENABLED", "true").lower() == "false":
        _TRACING_CONFIGURED = True
        return
    if _TRACING_CONFIGURED:
        return

    try:
        from openinference.instrumentation import TraceConfig
        from openinference.instrumentation.llama_index import LlamaIndexInstrumentor
        from openinference.instrumentation.openai import OpenAIInstrumentor
        from opentelemetry import trace
        from phoenix.otel import register
    except ImportError:
        # Tracing is an optional deployment extra. A production image without
        # that extra must still be able to start.
        _TRACING_CONFIGURED = True
        return

    resolved_project_name = project_name or os.environ.get(
        "PHOENIX_PROJECT_NAME", "ai-pr-review-assistant"
    )
    include_content = observability_include_content
    if include_content is None:
        include_content = os.environ.get("OBSERVABILITY_INCLUDE_CONTENT", "").lower() == "true"
    endpoint = _normalize_collector_endpoint(
        os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    )
    tracer_provider = register(
        project_name=resolved_project_name,
        endpoint=endpoint,
        protocol="http/protobuf",
    )
    OpenAIInstrumentor().instrument(
        tracer_provider=tracer_provider,
        config=TraceConfig(
            hide_inputs=not include_content,
            hide_outputs=not include_content,
            hide_input_text=not include_content,
            hide_output_text=not include_content,
        ),
    )
    LlamaIndexInstrumentor().instrument(
        tracer_provider=tracer_provider,
        config=TraceConfig(
            hide_inputs=not include_content,
            hide_outputs=not include_content,
            hide_input_text=not include_content,
            hide_output_text=not include_content,
        ),
    )
    _TRACER = trace.get_tracer("review-bot.rag")
    _TRACING_CONFIGURED = True


def rag_span(name: str, **attributes: Any) -> ContextManager[Any]:
    """Return a root RAG span, or a no-op context when tracing is not installed."""

    if _TRACER is None:
        return nullcontext()
    span = _TRACER.start_span(name, attributes=_safe_attributes(attributes))
    return _private_span(span)


def _use_span(span: Any) -> ContextManager[Any]:
    from opentelemetry import trace

    return trace.use_span(span, end_on_exit=True, record_exception=False, set_status_on_exception=False)


def _safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    return safe_attributes(attributes)


def set_span_attributes(span: Any, attributes: dict[str, Any]) -> None:
    if span is not None:
        for key, value in safe_attributes(attributes).items():
            span.set_attribute(key, value)


def _normalize_collector_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        return endpoint
    if parsed.path not in {"", "/"}:
        return endpoint
    return urlunparse(parsed._replace(path="/v1/traces"))


class _PrivateSpan:
    def __init__(self, span: Any):
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        set_span_attributes(self._span, {key: value})

    def set_attributes(self, values: dict[str, Any]) -> None:
        set_span_attributes(self._span, values)


@contextmanager
def _private_span(span: Any):
    with _use_span(span):
        yield _PrivateSpan(span)
