from __future__ import annotations

import json
import re
from typing import Any


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL | re.I)


def parse_json_object(text: str) -> dict[str, Any]:
    payload = _strip_markdown_fence(text)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM response is not valid JSON") from exc

    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON must be an object")
    return parsed


def extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    choices = _get(response, "choices") or []
    if not choices:
        return []

    message = _get(choices[0], "message")
    tool_calls = _get(message, "tool_calls") or []
    parsed_calls: list[dict[str, Any]] = []
    for call in tool_calls:
        function = _get(call, "function") or {}
        raw_arguments = _get(function, "arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError as exc:
            raise ValueError("Tool call arguments are not valid JSON") from exc

        if not isinstance(arguments, dict):
            raise ValueError("Tool call arguments must be a JSON object")

        parsed_calls.append(
            {
                "id": _get(call, "id"),
                "type": _get(call, "type") or "function",
                "name": _get(function, "name"),
                "arguments": arguments,
            }
        )
    return parsed_calls


def _strip_markdown_fence(text: str) -> str:
    match = _FENCE_RE.match(text)
    if match:
        return match.group("body").strip()
    return text.strip()


def _get(value: Any, field_name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(field_name)
    return getattr(value, field_name, None)
