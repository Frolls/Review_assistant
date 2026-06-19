from __future__ import annotations

import re
from typing import Final

from app.observability.pii import redact_pii


EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RU_RE: Final = re.compile(
    r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)
PASSPORT_RU_RE: Final = re.compile(r"\b\d{4}\s?\d{6}\b")

BLOCKED_MODERATION_TERMS: Final[tuple[str, ...]] = (
    "ненавижу людей",
    "hate humans",
    "kill all humans",
)


def filter_output(answer: str, system_prompt: str, canary: str) -> str:
    """Block system prompt leakage and mask PII before the answer leaves the LLM layer."""
    normalized_answer = _normalize(answer)

    if canary and canary.lower() in normalized_answer.lower():
        raise ValueError("system_prompt leakage: canary detected")

    normalized_prompt = _normalize(system_prompt)
    prompt_head = normalized_prompt[:80]
    if prompt_head and prompt_head.lower() in normalized_answer.lower():
        raise ValueError("system_prompt leakage: prefix detected")

    if _looks_disallowed(normalized_answer):
        raise ValueError("moderation blocked: disallowed harassment or hate content")

    masked = redact_pii(answer)
    masked = EMAIL_RE.sub("[EMAIL]", masked)
    masked = PHONE_RU_RE.sub("[PHONE_RU]", masked)
    return PASSPORT_RU_RE.sub("[PASSPORT]", masked)


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _looks_disallowed(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in BLOCKED_MODERATION_TERMS)

