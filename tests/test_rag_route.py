from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.deps.providers import get_rag_service
from app.routers.rag import router


class FakeRAGService:
    async def answer(self, question: str) -> dict:
        return {
            "answer": f"answer for {question}",
            "top_score": 0.75,
            "confident": True,
            "sources": [
                {
                    "id": 1,
                    "file_name": "source.md",
                    "page": 3,
                    "score": 0.75,
                    "snippet": "source text",
                }
            ],
        }


@pytest.mark.asyncio
async def test_rag_query_returns_answer_with_sources() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_rag_service] = lambda: FakeRAGService()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/rag/query", json={"question": "Что проверить?"})

    assert response.status_code == 200
    assert response.json() == {
        "answer": "answer for Что проверить?",
        "top_score": 0.75,
        "confident": True,
        "sources": [
            {
                "id": 1,
                "file_name": "source.md",
                "page": 3,
                "score": 0.75,
                "snippet": "source text",
            }
        ],
    }


@pytest.mark.asyncio
async def test_rag_query_returns_503_when_index_is_unavailable() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_rag_service] = lambda: None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/rag/query", json={"question": "Что проверить?"})

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "rag_unavailable",
            "message": "RAG index is unavailable.",
        }
    }
