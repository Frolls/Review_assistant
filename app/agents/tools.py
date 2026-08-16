"""LangChain tools backed by the application's services."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from app.services.rag import RAGService, UNKNOWN_ANSWER


def make_search_knowledge_base_tool(rag_service: RAGService) -> StructuredTool:
    """Adapt the production RAG service to the agent tool interface."""

    async def search(query: str) -> str:
        prepared = await rag_service.prepare(query)
        if not prepared.confident:
            payload: dict[str, Any] = {
                "confident": False,
                "answer": UNKNOWN_ANSWER,
                "top_score": prepared.top_score,
                "sources": [],
            }
        else:
            payload = {
                "confident": True,
                "top_score": prepared.top_score,
                "sources": [
                    {
                        "id": source["id"],
                        "file_name": source["file_name"],
                        "page": source["page"],
                        "score": source["score"],
                        "text": text,
                    }
                    for source, text in zip(prepared.sources, prepared.retrieved_contexts)
                ],
            }
        return json.dumps(payload, ensure_ascii=False)

    return StructuredTool.from_function(
        coroutine=search,
        name="search_knowledge_base",
        description="Ищет подтверждённые факты в корпоративной базе знаний.",
    )
