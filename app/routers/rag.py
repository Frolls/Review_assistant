from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.deps.providers import RAGServiceDep
from app.routers.responses import RAG_RESPONSES
from app.schemas.rag import RAGQueryRequest, RAGQueryResponse


router = APIRouter(tags=["rag"])


@router.post(
    "/rag/query",
    response_model=RAGQueryResponse,
    summary="Answer a question using the RAG knowledge corpus",
    responses=RAG_RESPONSES,
)
async def rag_query(payload: RAGQueryRequest, rag_service: RAGServiceDep) -> RAGQueryResponse:
    if rag_service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "rag_unavailable",
                "message": "RAG index is unavailable.",
            },
        )
    result = await rag_service.answer(payload.question)
    return RAGQueryResponse.model_validate(result)
