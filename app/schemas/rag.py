from __future__ import annotations

from pydantic import BaseModel, Field


class RAGQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class RAGSource(BaseModel):
    text: str
    source: str | None = None
    score: float | None = None


class RAGQueryResponse(BaseModel):
    answer: str
    top_score: float | None
    sources: list[RAGSource]
