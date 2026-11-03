"""One content policy for custom traces and structured logs (deny by default)."""
import hashlib
import os
from typing import Any

_include_content: bool | None = None
_TECHNICAL_STRINGS = frozenset({
    "event", "level", "timestamp", "request_id", "chat_id", "user_id", "path", "method",
    "model", "rag.model", "decision", "status", "direction", "mode", "provider",
    "prompt_version", "error_type", "operation", "finish_reason", "thread_id",
})


def configure_content_policy(include: bool) -> None:
    global _include_content
    _include_content = include


def include_content() -> bool:
    if _include_content is not None:
        return _include_content
    return os.environ.get("OBSERVABILITY_INCLUDE_CONTENT", "false").lower() == "true"


def content_attributes(key: str, text: str) -> dict[str, Any]:
    if include_content():
        return {key: text}
    return {f"{key}.sha256": hashlib.sha256(text.encode()).hexdigest(), f"{key}.length": len(text)}


def safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in attributes.items():
        if isinstance(value, str):
            if key in _TECHNICAL_STRINGS or key.endswith(".sha256"):
                result[key] = value
            else:
                result.update(content_attributes(key, value))
        elif isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, dict):
            result.update(safe_attributes({f"{key}.{k}": v for k, v in value.items()}))
        elif isinstance(value, (list, tuple)):
            result.update(safe_attributes({f"{key}.{i}": v for i, v in enumerate(value)}))
    return result
