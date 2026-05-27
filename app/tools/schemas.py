from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.prompts.loader import load_tool_prompt


SEARCH_REVIEW_KB_NAME = "search_review_kb"
SEARCH_REVIEW_KB_DESCRIPTION = load_tool_prompt("search_review_kb.md")


class SearchReviewKbArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        description="Поисковый запрос по базе знаний для ревью.",
    )
    max_results: int = Field(
        ge=1,
        le=5,
        description="Сколько релевантных фрагментов вернуть.",
    )


SEARCH_REVIEW_KB_PARAMETERS: dict[str, Any] = SearchReviewKbArguments.model_json_schema()

SEARCH_REVIEW_KB_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_REVIEW_KB_NAME,
        "description": SEARCH_REVIEW_KB_DESCRIPTION,
        "parameters": SEARCH_REVIEW_KB_PARAMETERS,
        "strict": True,
    },
}

_TOOL_ARGUMENT_MODELS = {SEARCH_REVIEW_KB_NAME: SearchReviewKbArguments}


def get_tools() -> list[dict[str, Any]]:
    return [SEARCH_REVIEW_KB_TOOL]


def validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name not in _TOOL_ARGUMENT_MODELS:
        raise ValueError(f"Unknown tool schema: {tool_name}")

    validated_arguments = _TOOL_ARGUMENT_MODELS[tool_name].model_validate(arguments)
    return validated_arguments.model_dump()
