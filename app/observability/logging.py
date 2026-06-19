from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def setup_logging(level: str | int = "INFO") -> None:
    level_number = _coerce_level(level)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level_number,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_pii_processor,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_number),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return int(getattr(logging, str(level).upper(), logging.INFO))


def redact_pii_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    from app.observability.pii import redact_pii

    return {key: _redact_value(value, redact_pii) for key, value in event_dict.items()}


def _redact_value(value: Any, redact: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item, redact) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, redact) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, redact) for item in value)
    return value


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
