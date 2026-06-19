from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final


MAX_INPUT_CHARS: Final[int] = 4_000
NON_PRINTABLE_RATIO_LIMIT: Final[float] = 0.10
SUSPICIOUS_UNICODE_RATIO_LIMIT: Final[float] = 0.35

INJECTION_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(
        r"\bignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bdisregard\s+(the\s+)?(system|previous|above|prior)\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(a|an|the|dan|do anything now)\b", re.IGNORECASE),
    re.compile(r"\bforget\s+(everything|all|previous|prior)\b", re.IGNORECASE),
    re.compile(r"\b(jailbroken|developer mode|godmode|do anything now)\b", re.IGNORECASE),
    re.compile(r"\breveal\s+(the\s+)?(system|developer)\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bbase64\b|(?:[A-Za-z0-9+/]{80,}={0,2})", re.IGNORECASE),
]


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None
    rule: str | None = None


def validate_input(text: str) -> ValidationResult:
    """Return a blocking decision for prompts that look like direct prompt attacks."""
    if len(text) > MAX_INPUT_CHARS:
        return ValidationResult(False, "input too long", rule="length")

    non_printable = sum(1 for char in text if not char.isprintable() and char not in "\n\r\t")
    if non_printable / max(len(text), 1) > NON_PRINTABLE_RATIO_LIMIT:
        return ValidationResult(False, "high non-printable ratio", rule="encoding")

    suspicious_unicode = sum(1 for char in text if _is_suspicious_unicode(char))
    if suspicious_unicode / max(len(text), 1) > SUSPICIOUS_UNICODE_RATIO_LIMIT:
        return ValidationResult(False, "high suspicious unicode ratio", rule="encoding")

    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return ValidationResult(
                False,
                f"matched pattern {pattern.pattern}",
                rule="injection",
            )

    return ValidationResult(True)


def _is_suspicious_unicode(char: str) -> bool:
    if char.isspace() or char.isascii():
        return False

    name = unicodedata.name(char, "")
    allowed_blocks = (
        "CYRILLIC",
        "LATIN",
        "DIGIT",
        "SPACE",
        "PUNCTUATION",
        "HYPHEN",
        "DASH",
        "QUOTATION",
    )
    return not any(block in name for block in allowed_blocks)

