from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

from openinference.instrumentation import TraceConfig
from openinference.instrumentation.openai import OpenAIInstrumentor
from phoenix.otel import register


_TRACING_CONFIGURED = False


def setup_tracing(
    project_name: str | None = None,
    *,
    observability_include_content: bool | None = None,
) -> None:
    global _TRACING_CONFIGURED
    if _TRACING_CONFIGURED:
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
    _TRACING_CONFIGURED = True


def _normalize_collector_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        return endpoint
    if parsed.path not in {"", "/"}:
        return endpoint
    return urlunparse(parsed._replace(path="/v1/traces"))
