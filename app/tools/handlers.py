from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.tools.schemas import SEARCH_REVIEW_KB_NAME, validate_tool_arguments


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[0-9A-Za-zА-Яа-я_]+", text.lower()))


def _load_review_kb() -> list[dict[str, Any]]:
    kb_path = Path(get_settings().review_kb_path)
    with kb_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def search_review_kb(query: str, max_results: int = 3) -> dict[str, Any]:
    entries = _load_review_kb()
    query_tokens = _tokenize(query)
    scored_matches: list[dict[str, Any]] = []

    for entry in entries:
        searchable_text = " ".join(
            [
                entry.get("source", ""),
                entry.get("topic", ""),
                " ".join(entry.get("tags", [])),
                entry.get("content", ""),
            ]
        )
        entry_tokens = _tokenize(searchable_text)
        overlap_score = len(query_tokens & entry_tokens)
        if query.lower() in searchable_text.lower():
            overlap_score += 2

        if overlap_score == 0:
            continue

        scored_matches.append(
            {
                "source": entry["source"],
                "topic": entry["topic"],
                "excerpt": entry["content"],
                "score": overlap_score,
            }
        )

    scored_matches.sort(key=lambda item: item["score"], reverse=True)
    matches = scored_matches[:max_results]
    return {
        "query": query,
        "match_count": len(matches),
        "matches": matches,
    }


def execute_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    validated_arguments = validate_tool_arguments(tool_name, arguments)

    if tool_name == SEARCH_REVIEW_KB_NAME:
        return search_review_kb(**validated_arguments)

    raise ValueError(f"Unsupported tool: {tool_name}")
