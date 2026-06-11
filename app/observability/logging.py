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


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
