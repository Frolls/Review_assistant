from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

from openinference.instrumentation.openai import OpenAIInstrumentor
from phoenix.otel import register


_TRACING_CONFIGURED = False


def setup_tracing(project_name: str = "diploma-fastapi") -> None:
    global _TRACING_CONFIGURED
    if _TRACING_CONFIGURED:
        return

    endpoint = _normalize_collector_endpoint(
        os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    )
    tracer_provider = register(
        project_name=project_name,
        endpoint=endpoint,
        protocol="http/protobuf",
    )
    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
    _TRACING_CONFIGURED = True


def _normalize_collector_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        return endpoint
    if parsed.path not in {"", "/"}:
        return endpoint
    return urlunparse(parsed._replace(path="/v1/traces"))
