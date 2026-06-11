from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import re
import time
from functools import lru_cache
from typing import Any

from app.observability.logging import get_logger


PII_PATTERNS = {
    "EMAIL": re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "PHONE_RU": re.compile(
        r"(?<!\w)(?:\+7|8)[\s\-]*\(?\d{3}\)?[\s\-]*"
        r"\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\w)"
    ),
    "CARD": re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"),
    "INN": re.compile(r"\b(?:\d{10}|\d{12})\b"),
    "PASSPORT": re.compile(r"\b\d{2}\s?\d{2}\s?\d{6}\b"),
}

_LONG_PROMPT_THRESHOLD = 2_000
_logger = get_logger(__name__)


def redact_pii(text: str) -> str:
    return redact_pii_regex(text)


def redact_pii_regex(text: str) -> str:
    redacted = text
    for name, pattern in PII_PATTERNS.items():
        redacted = pattern.sub(f"[{name}]", redacted)
    return redacted


async def redact_pii_for_log(text: str) -> str:
    redacted = redact_pii_regex(text)
    if len(text) < _LONG_PROMPT_THRESHOLD:
        return redacted

    try:
        asyncio.create_task(_redact_long_prompt_in_background(redacted, prompt_hash(text)))
    except RuntimeError:
        return _redact_with_presidio(redacted)
    return redacted


def prompt_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def _redact_long_prompt_in_background(text: str, digest: str) -> None:
    started_at = time.perf_counter()
    redacted = await asyncio.to_thread(_redact_with_presidio, text)
    latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
    _logger.info(
        "pii_redaction_completed",
        prompt_hash=digest,
        latency_ms=latency_ms,
        mode="background",
        prompt_preview=redacted[:120],
    )


def _redact_with_presidio(text: str) -> str:
    analyzer, anonymizer, operator_config = _load_presidio()
    if analyzer is None or anonymizer is None or operator_config is None:
        return text

    try:
        results = analyzer.analyze(text=text, language="ru", entities=["PERSON"])
        anonymized = anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators={"PERSON": operator_config("replace", {"new_value": "[PERSON]"})},
        )
    except Exception:
        return text
    return anonymized.text


@lru_cache(maxsize=1)
def _load_presidio() -> tuple[Any | None, Any | None, Any | None]:
    if importlib.util.find_spec("ru_core_news_md") is None:
        return None, None, None

    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig
    except Exception:
        return None, None, None

    try:
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "ru", "model_name": "ru_core_news_md"}],
            }
        )
        nlp_engine = provider.create_engine()
        analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["ru"])
        return analyzer, AnonymizerEngine(), OperatorConfig
    except Exception:
        return None, None, None
