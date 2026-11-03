from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RAGQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class RAGSource(BaseModel):
    id: int
    file_name: str
    page: int | str | None = None
    score: float | None = None
    snippet: str


class RAGQueryResponse(BaseModel):
    answer: str
    status: Literal["answered", "clarify", "refused", "blocked"] = "answered"
    top_score: float | None
    confident: bool
    sources: list[RAGSource]
