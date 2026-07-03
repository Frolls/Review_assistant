from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.observability.logging import get_logger
from app.observability.pii import prompt_hash, redact_pii


logger = get_logger(__name__)

Direction = Literal["input", "output"]


@dataclass(frozen=True)
class ModerationResult:
    allowed: bool
    categories: list[str]
    reasons: list[str]
    blocked_by: str


@dataclass(frozen=True)
class KeywordRule:
    category: str
    pattern: re.Pattern[str]
    reason: str


class ModerationService:
    def __init__(
        self,
        *,
        openai_client: Any | None = None,
        keywords_path: Path | str | None = None,
        openai_enabled: bool = False,
        model: str = "omni-moderation-latest",
        preview_limit: int = 160,
    ) -> None:
        self.openai_client = openai_client
        self.openai_enabled = openai_enabled
        self.model = model
        self.preview_limit = preview_limit
        payload = _load_keywords(Path(keywords_path) if keywords_path is not None else _default_path())
        self.rules: dict[str, list[KeywordRule]] = {
            "input": _compile_rules(payload.get("input", [])),
            "output": _compile_rules(payload.get("output", [])),
        }
        self.thresholds = _coerce_thresholds(payload.get("thresholds", {}))

    async def check_input(self, content: str) -> ModerationResult:
        return await self._check(content, "input")

    async def check_output(self, content: str) -> ModerationResult:
        return await self._check(content, "output")

    async def _check(self, content: str, direction: Direction) -> ModerationResult:
        keyword_result = self._check_keywords(content, direction)
        if not keyword_result.allowed:
            self._log_incident(content, keyword_result, direction)
            return keyword_result

        if not self.openai_enabled:
            return ModerationResult(True, [], [], "")

        openai_result = await self._check_openai(content)
        if not openai_result.allowed:
            self._log_incident(content, openai_result, direction)
        return openai_result

    def _check_keywords(self, content: str, direction: Direction) -> ModerationResult:
        categories: list[str] = []
        reasons: list[str] = []
        for rule in self.rules[direction]:
            if not rule.pattern.search(content):
                continue
            categories.append(rule.category)
            reasons.append(rule.reason)

        if not categories:
            return ModerationResult(True, [], [], "")
        return ModerationResult(
            allowed=False,
            categories=_unique(categories),
            reasons=_unique(reasons),
            blocked_by="keyword",
        )

    async def _check_openai(self, content: str) -> ModerationResult:
        moderations = getattr(self.openai_client, "moderations", None)
        create = getattr(moderations, "create", None)
        if create is None:
            return ModerationResult(True, [], [], "")

        try:
            moderation = await create(model=self.model, input=content)
        except Exception as exc:
            logger.warning("moderation.openai_unavailable", error=str(exc))
            return ModerationResult(True, [], [], "")

        results = getattr(moderation, "results", None) or []
        if not results:
            return ModerationResult(True, [], [], "")

        result = results[0]
        flagged = bool(getattr(result, "flagged", False))
        categories = _flagged_categories(getattr(result, "categories", None))
        threshold_categories = _threshold_categories(
            getattr(result, "category_scores", None),
            self.thresholds,
        )
        blocked_categories = _unique([*categories, *threshold_categories])

        if flagged or blocked_categories:
            return ModerationResult(
                allowed=False,
                categories=blocked_categories or ["unknown"],
                reasons=["OpenAI moderation flagged content"],
                blocked_by="openai",
            )
        return ModerationResult(True, [], [], "")

    def _log_incident(
        self,
        content: str,
        result: ModerationResult,
        direction: Direction,
    ) -> None:
        logger.warning(
            "moderation.blocked",
            direction=direction,
            text_hash=prompt_hash(content),
            text_preview=redact_pii(content)[: self.preview_limit],
            categories=result.categories,
            reasons=result.reasons,
            blocked_by=result.blocked_by,
        )


def _default_path() -> Path:
    return Path(__file__).with_name("moderation_keywords.yaml")


def _load_keywords(path: Path) -> dict[str, Any]:
    if not path.exists():
        logger.warning("moderation.keyword_file_missing", path=str(path))
        return {"input": [], "output": [], "thresholds": {}}

    try:
        import yaml  # type: ignore[import-untyped]
    except Exception:
        return _parse_simple_yaml(path.read_text(encoding="utf-8"))

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"input": [], "output": [], "thresholds": {}}
    section: str | None = None
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not raw_line.startswith(" ") and line.endswith(":"):
            section = line[:-1]
            current = None
            continue
        if section in {"input", "output"}:
            if line.startswith("- "):
                current = {}
                payload[section].append(current)
                line = line[2:].strip()
            if current is not None and ": " in line:
                key, value = line.split(": ", 1)
                current[key.strip()] = value.strip().strip('"').strip("'")
            continue
        if section == "thresholds" and ": " in line:
            key, value = line.split(": ", 1)
            try:
                payload["thresholds"][key.strip()] = float(value)
            except ValueError:
                continue
    return payload


def _compile_rules(raw_rules: object) -> list[KeywordRule]:
    if not isinstance(raw_rules, list):
        return []

    compiled: list[KeywordRule] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            continue
        category = str(raw_rule.get("category") or "unknown")
        pattern = str(raw_rule.get("pattern") or "")
        reason = str(raw_rule.get("reason") or f"matched {category}")
        if not pattern:
            continue
        try:
            compiled.append(KeywordRule(category, re.compile(pattern), reason))
        except re.error as exc:
            logger.warning("moderation.keyword_rule_invalid", category=category, error=str(exc))
    return compiled


def _coerce_thresholds(raw_thresholds: object) -> dict[str, float]:
    if not isinstance(raw_thresholds, dict):
        return {}
    thresholds: dict[str, float] = {}
    for key, value in raw_thresholds.items():
        try:
            thresholds[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return thresholds


def _flagged_categories(categories: object) -> list[str]:
    if categories is None:
        return []
    if isinstance(categories, dict):
        return [str(key) for key, value in categories.items() if value]

    found: list[str] = []
    for name in _known_categories():
        attr_name = name.replace("/", "_").replace("-", "_")
        if bool(getattr(categories, name, False)) or bool(getattr(categories, attr_name, False)):
            found.append(name)
    return found


def _threshold_categories(scores: object, thresholds: dict[str, float]) -> list[str]:
    found: list[str] = []
    for category, threshold in thresholds.items():
        score = _category_value(scores, category)
        if score is not None and score >= threshold:
            found.append(category)
    return found


def _category_value(source: object, category: str) -> float | None:
    if source is None:
        return None
    if isinstance(source, dict):
        value = source.get(category)
    else:
        attr_name = category.replace("/", "_").replace("-", "_")
        value = getattr(source, category, None)
        if value is None:
            value = getattr(source, attr_name, None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _known_categories() -> tuple[str, ...]:
    return (
        "harassment",
        "harassment/threatening",
        "hate",
        "hate/threatening",
        "illicit",
        "illicit/violent",
        "self-harm",
        "self-harm/instructions",
        "self-harm/intent",
        "sexual",
        "sexual/minors",
        "violence",
        "violence/graphic",
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
