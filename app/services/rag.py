from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Sequence
from uuid import UUID

from openai import AsyncOpenAI

from app.core.config import Settings, get_settings
from app.observability.logging import get_logger
from app.observability.tracing import rag_span
from app.services.embeddings import EmbeddingConfig, LlamaIndexEmbeddingAdapter
from app.services.reranker import BGEReranker


logger = get_logger(__name__)

UNKNOWN_ANSWER = "по базе не нашёл, могу эскалировать"
GROUNDING_INSTRUCTION = (
    "Ты корпоративный RAG-ассистент. Отвечай только по переданному контексту. "
    "Каждое фактическое утверждение сопровождай ссылкой на номер фрагмента: [1], [2]. "
    "Не цитируй номер, если фрагмент не подтверждает утверждение. "
    f"Если ответа в контексте нет, ответь ровно: «{UNKNOWN_ANSWER}»"
)
CONDENSE_PROMPT = (
    "Перепиши последний вопрос пользователя как самодостаточный поисковый запрос. "
    "Разреши местоимения и короткие продолжения по истории. Не отвечай на вопрос, "
    "не добавляй новых фактов. Верни только переписанный вопрос."
)


@dataclass(slots=True)
class PreparedRAG:
    original_question: str
    retrieval_question: str
    sources: list[dict[str, Any]]
    top_score: float | None
    confident: bool
    context: str
    retrieved_contexts: list[str]


class RAGService:
    """Retrieval-first RAG: score guard always runs before answer generation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._retriever: Any | None = None
        self._qdrant_client: Any | None = None
        self._llm = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url,
            timeout=settings.request_timeout,
        )
        self._reranker: BGEReranker | None = None

    async def build(self) -> None:
        await asyncio.to_thread(self._build_sync)

    async def prepare(
        self,
        question: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
        chat_id: UUID | str | None = None,
    ) -> PreparedRAG:
        if self._retriever is None:
            await self.build()

        retrieval_question = question
        if self.settings.rag_condense_enabled and history:
            retrieval_question = await self._condense(question, history, chat_id=chat_id)

        nodes = await asyncio.to_thread(self._retrieve_sync, retrieval_question)
        top_score = _top_score(nodes)
        confident = (
            top_score is not None and top_score >= self.settings.rag_score_threshold
        )
        if not confident:
            logger.info(
                "rag.score_guard_refusal",
                chat_id=str(chat_id) if chat_id is not None else None,
                top_score=top_score,
                threshold=self.settings.rag_score_threshold,
                retrieval_question=retrieval_question[:200],
            )
            return PreparedRAG(
                original_question=question,
                retrieval_question=retrieval_question,
                sources=[],
                top_score=top_score,
                confident=False,
                context="",
                retrieved_contexts=[_node_text(node) for node in nodes],
            )

        ranked_nodes = await asyncio.to_thread(
            self._rerank_sync,
            retrieval_question,
            nodes,
        )
        sources = [_source_payload(node, index) for index, node in enumerate(ranked_nodes, 1)]
        retrieved_contexts = [_node_text(node) for node in ranked_nodes]
        context = "\n\n".join(
            f"[{source['id']}] Файл: {source['file_name']}; "
            f"страница: {source['page'] or '—'}\n{full_text}"
            for source, full_text in zip(sources, retrieved_contexts)
        )
        return PreparedRAG(
            original_question=question,
            retrieval_question=retrieval_question,
            sources=sources,
            top_score=top_score,
            confident=True,
            context=context,
            retrieved_contexts=retrieved_contexts,
        )

    async def answer(
        self,
        question: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
        chat_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        with rag_span(
            "rag.answer",
            **{"input.value": question, "rag.model": self.settings.default_model},
        ) as span:
            prepared = await self.prepare(question, history=history, chat_id=chat_id)
            answer = await self._answer_prepared(prepared, history=history)
            _set_span_attributes(
                span,
                {
                    "output.value": answer,
                    "rag.retrieved_contexts.count": len(prepared.retrieved_contexts),
                    "rag.confident": prepared.confident,
                    "rag.top_score": prepared.top_score,
                },
            )
            return _result(answer, prepared)

    async def evaluate_inputs(self, question: str) -> dict[str, Any]:
        """Run one production RAG pass and expose the full retrieved chunks for eval."""

        started_at = time.perf_counter()
        with rag_span(
            "rag.evaluate",
            **{"input.value": question, "rag.model": self.settings.default_model},
        ) as span:
            prepared = await self.prepare(question)
            answer = await self._answer_prepared(prepared)
            latency_ms = round((time.perf_counter() - started_at) * 1000, 3)
            result = {
                "answer": answer,
                "retrieved_contexts": prepared.retrieved_contexts,
                "latency_ms": latency_ms,
                "top_score": _rounded_score(prepared.top_score),
                "confident": prepared.confident,
            }
            _set_span_attributes(
                span,
                {
                    "output.value": answer,
                    "rag.retrieved_contexts.count": len(prepared.retrieved_contexts),
                    "rag.latency_ms": latency_ms,
                    "rag.confident": prepared.confident,
                    "rag.top_score": prepared.top_score,
                },
            )
            return result

    async def stream_answer(
        self,
        question: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
        chat_id: UUID | str | None = None,
    ) -> tuple[PreparedRAG, AsyncIterator[str]]:
        prepared = await self.prepare(question, history=history, chat_id=chat_id)
        if not prepared.confident:
            return prepared, _single_chunk(UNKNOWN_ANSWER)

        stream = await self._llm.chat.completions.create(
            model=self.settings.default_model,
            messages=self.generation_messages(prepared, history=history),
            temperature=0,
            **_ollama_model_args(self.settings.openai_base_url),
            stream=True,
        )

        async def tokens() -> AsyncIterator[str]:
            async for chunk in stream:
                text = _stream_delta(chunk)
                if text:
                    yield text

        return prepared, tokens()

    def generation_messages(
        self,
        prepared: PreparedRAG,
        *,
        history: Sequence[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": f"{GROUNDING_INSTRUCTION}\n\nКонтекст:\n{prepared.context}",
            }
        ]
        messages.extend(_text_messages(history or []))
        if not messages or not _same_last_user(messages, prepared.original_question):
            messages.append({"role": "user", "content": prepared.original_question})
        return messages

    async def _answer_prepared(
        self,
        prepared: PreparedRAG,
        *,
        history: Sequence[dict[str, Any]] | None = None,
    ) -> str:
        if not prepared.confident:
            return UNKNOWN_ANSWER
        response = await self._llm.chat.completions.create(
            model=self.settings.default_model,
            messages=self.generation_messages(prepared, history=history),
            temperature=0,
            **_ollama_model_args(self.settings.openai_base_url),
        )
        answer = _completion_text(response) or UNKNOWN_ANSWER
        return _ensure_source_marker(answer, prepared)

    async def close(self) -> None:
        await self._llm.close()
        client = self._qdrant_client
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                await asyncio.to_thread(close)

    def _build_sync(self) -> None:
        if self._retriever is not None:
            return
        try:
            from llama_index.core import VectorStoreIndex
            from llama_index.vector_stores.qdrant import QdrantVectorStore
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install llama-index, llama-index-vector-stores-qdrant and qdrant-client."
            ) from exc

        self._qdrant_client = QdrantClient(
            url=self.settings.qdrant_url,
            api_key=self.settings.qdrant_api_key,
        )
        if not self._qdrant_client.collection_exists(self.settings.rag_collection):
            raise RuntimeError(
                f"RAG collection {self.settings.rag_collection!r} is absent. "
                "Run `python scripts/ingest.py data/` first."
            )
        points_count = int(
            getattr(
                self._qdrant_client.get_collection(self.settings.rag_collection),
                "points_count",
                0,
            )
            or 0
        )
        if points_count == 0:
            raise RuntimeError(
                f"RAG collection {self.settings.rag_collection!r} is empty. "
                "Run `python scripts/ingest.py data/` first."
            )

        vector_store = QdrantVectorStore(
            client=self._qdrant_client,
            collection_name=self.settings.rag_collection,
        )
        index = VectorStoreIndex.from_vector_store(
            vector_store=vector_store,
            embed_model=LlamaIndexEmbeddingAdapter(
                EmbeddingConfig.from_settings(self.settings)
            ),
        )
        self._retriever = index.as_retriever(
            similarity_top_k=self.settings.rag_similarity_top_k
        )

    def _retrieve_sync(self, question: str) -> list[Any]:
        if self._retriever is None:
            raise RuntimeError("RAGService is not built")
        return list(self._retriever.retrieve(question))

    def _rerank_sync(self, question: str, nodes: list[Any]) -> list[Any]:
        if not self.settings.rag_reranker_enabled:
            return nodes[: self.settings.rag_reranker_top_n]
        try:
            if self._reranker is None:
                self._reranker = BGEReranker(self.settings.rag_reranker_model)
            return self._reranker.rerank(
                question,
                nodes,
                top_n=self.settings.rag_reranker_top_n,
            )
        except Exception as exc:
            logger.warning("rag.reranker_unavailable", error=str(exc))
            return nodes[: self.settings.rag_reranker_top_n]

    async def _condense(
        self,
        question: str,
        history: Sequence[dict[str, Any]],
        *,
        chat_id: UUID | str | None,
    ) -> str:
        transcript = "\n".join(
            f"{item.get('role', 'user')}: {_message_text(item.get('content'))}"
            for item in history[-self.settings.chat_context_window :]
            if item.get("role") in {"user", "assistant"}
            and _message_text(item.get("content"))
        )
        response = await self._llm.chat.completions.create(
            model=self.settings.default_model,
            messages=[
                {"role": "system", "content": CONDENSE_PROMPT},
                {
                    "role": "user",
                    "content": f"История chat_id={chat_id or 'one-shot'}:\n{transcript}\n"
                    f"Последний вопрос: {question}",
                },
            ],
            temperature=0,
            max_tokens=256,
            **_ollama_model_args(self.settings.openai_base_url),
        )
        condensed = _completion_text(response).strip() or question
        anchor = _last_user_text(history)
        if anchor and _is_context_dependent_followup(question):
            # Local models occasionally return an empty rewrite or drop the
            # subject while resolving the pronoun. Preserve the previous user
            # question as a retrieval anchor without another LLM call.
            condensed = f"{condensed}\nПредыдущий вопрос: {anchor}"
        if condensed != question:
            logger.info(
                "rag.query_condensed",
                chat_id=str(chat_id) if chat_id is not None else None,
                original=question[:200],
                condensed=condensed[:200],
            )
        return condensed


def _source_payload(node_with_score: Any, citation_id: int) -> dict[str, Any]:
    node = getattr(node_with_score, "node", node_with_score)
    metadata = getattr(node, "metadata", None) or {}
    text = _node_text(node_with_score)
    page = metadata.get("page") or metadata.get("page_label")
    try:
        page = int(page) if page is not None else None
    except (TypeError, ValueError):
        page = str(page)
    return {
        "id": citation_id,
        "file_name": _source_name(metadata),
        "page": page,
        "score": _rounded_score(getattr(node_with_score, "score", None)),
        "snippet": text[:700],
    }


def _node_text(node_with_score: Any) -> str:
    node = getattr(node_with_score, "node", node_with_score)
    text = getattr(node, "text", "")
    if not text:
        get_content = getattr(node, "get_content", None)
        text = get_content() if get_content is not None else ""
    return str(text).strip()


def _result(answer: str, prepared: PreparedRAG) -> dict[str, Any]:
    return {
        "answer": answer,
        "top_score": _rounded_score(prepared.top_score),
        "confident": prepared.confident,
        "sources": prepared.sources,
    }


def _top_score(nodes: list[Any]) -> float | None:
    scores = [getattr(node, "score", None) for node in nodes]
    numeric = [float(score) for score in scores if score is not None]
    return max(numeric) if numeric else None


def _rounded_score(score: float | None) -> float | None:
    return round(float(score), 4) if score is not None else None


def _set_span_attributes(span: Any, attributes: dict[str, Any]) -> None:
    if span is None:
        return
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def _source_name(metadata: dict[str, Any]) -> str:
    for key in ("file_name", "source", "file_path"):
        value = metadata.get(key)
        if value:
            return Path(str(value)).name
    return "unknown"


def _completion_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    return _message_text(getattr(choices[0].message, "content", ""))


def _ollama_model_args(base_url: str) -> dict[str, Any]:
    if "11434" in base_url or "ollama" in base_url.casefold():
        return {"extra_body": {"think": False}}
    return {}


def _ensure_source_marker(answer: str, prepared: PreparedRAG) -> str:
    """Keep the answer auditable when a local model ignores citation syntax."""

    if (
        not prepared.confident
        or not prepared.sources
        or answer == UNKNOWN_ANSWER
        or re.search(r"\[\d+\]", answer)
    ):
        return answer
    return f"{answer.rstrip()}\n\nИсточник: [1]"


def _stream_delta(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    return _message_text(getattr(choices[0].delta, "content", ""))


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_message_text(item) for item in content)
    if isinstance(content, dict):
        return _message_text(content.get("text") or content.get("content"))
    return str(content)


def _text_messages(history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in history:
        content = _message_text(item.get("content"))
        if content and item.get("role") in {"system", "user", "assistant"}:
            result.append({"role": item["role"], "content": content})
    return result


def _same_last_user(messages: Sequence[dict[str, Any]], question: str) -> bool:
    return bool(
        messages
        and messages[-1].get("role") == "user"
        and _message_text(messages[-1].get("content")).strip() == question.strip()
    )


def _last_user_text(history: Sequence[dict[str, Any]]) -> str:
    for item in reversed(history):
        if item.get("role") == "user":
            text = _message_text(item.get("content")).strip()
            if text:
                return text
    return ""


def _is_context_dependent_followup(question: str) -> bool:
    if len(question) > 160:
        return False
    normalized = question.casefold().replace("ё", "е")
    markers = (
        "для них",
        "для него",
        "для нее",
        "а как",
        "а что",
        "а если",
        "а когда",
        "этим",
        "таких",
        "for them",
        "for it",
        "and how",
        "and what",
        "what about",
    )
    return any(marker in normalized for marker in markers)


async def _single_chunk(text: str) -> AsyncIterator[str]:
    yield text


async def _demo() -> None:
    service = RAGService(get_settings())
    try:
        result = await service.answer("Почему в Ansible task лучше избегать command и shell?")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(_demo())
