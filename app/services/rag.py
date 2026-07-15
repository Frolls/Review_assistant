from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings


UNKNOWN_ANSWER = (
    "В корпусе RAG не нашлось достаточно релевантной информации для ответа."
)
QA_PROMPT_TEMPLATE = (
    "Ниже приведён контекст из базы знаний для ревью кода.\n"
    "---------------------\n"
    "{context_str}\n"
    "---------------------\n"
    "Ответь на вопрос, опираясь только на этот контекст. "
    "Если в контексте нет ответа, честно скажи, что в корпусе RAG не нашлось "
    "достаточно релевантной информации. Не используй внешние знания и не выдумывай. "
    "Отвечай по-русски, кратко и по делу.\n"
    "Вопрос: {query_str}\n"
    "Ответ:"
)


class RAGService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._query_engine: Any | None = None
        self._client: Any | None = None

    async def build(self) -> None:
        await asyncio.to_thread(self._build_sync)

    async def answer(self, question: str) -> dict[str, Any]:
        if self._query_engine is None:
            await self.build()
        return await asyncio.to_thread(self._answer_sync, question)

    async def close(self) -> None:
        client = self._client
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                await asyncio.to_thread(close)

    def _build_sync(self) -> None:
        if self._query_engine is not None:
            return

        try:
            from llama_index.core import Settings as LlamaSettings
            from llama_index.core import (
                PromptTemplate,
                SimpleDirectoryReader,
                StorageContext,
                VectorStoreIndex,
            )
            from llama_index.core.base.llms.types import LLMMetadata, MessageRole
            from llama_index.core.node_parser import SentenceSplitter
            from llama_index.embeddings.openai import OpenAIEmbedding
            from llama_index.llms.openai import OpenAI
            from llama_index.vector_stores.qdrant import QdrantVectorStore
            from pydantic import PrivateAttr
            from qdrant_client import QdrantClient
            from qdrant_client.http.models import Distance, VectorParams
        except ImportError as exc:  # pragma: no cover - exercised in clean local envs.
            raise RuntimeError(
                "Install llama-index, llama-index-vector-stores-qdrant, "
                "llama-index-readers-file and qdrant-client to use RAGService."
            ) from exc

        input_dir = self._input_dir()
        if not input_dir.exists():
            raise FileNotFoundError(f"RAG input directory does not exist: {input_dir}")

        self._client = QdrantClient(
            url=self.settings.qdrant_url,
            api_key=self.settings.qdrant_api_key,
        )
        exists = bool(self._client.collection_exists(self.settings.rag_collection))
        if exists:
            current_size = _collection_vector_size(
                self._client.get_collection(self.settings.rag_collection)
            )
            if current_size != self.settings.embedding_dim:
                raise ValueError(
                    f"Qdrant collection {self.settings.rag_collection!r} has vector size "
                    f"{current_size}, but EMBEDDING_DIM={self.settings.embedding_dim}."
                )
        else:
            self._client.create_collection(
                collection_name=self.settings.rag_collection,
                vectors_config=VectorParams(
                    size=self.settings.embedding_dim,
                    distance=Distance.COSINE,
                ),
            )

        class OpenAICompatibleLLM(OpenAI):
            _context_window: int = PrivateAttr(default=8192)

            def __init__(self, *, context_window: int, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._context_window = context_window

            @property
            def metadata(self) -> LLMMetadata:
                return LLMMetadata(
                    context_window=self._context_window,
                    num_output=self.max_tokens or 512,
                    is_chat_model=True,
                    is_function_calling_model=False,
                    model_name=self.model,
                    system_role=MessageRole.SYSTEM,
                )

        LlamaSettings.llm = OpenAICompatibleLLM(
            model=self.settings.default_model,
            api_key=self.settings.openai_api_key.get_secret_value(),
            api_base=self.settings.openai_base_url,
            timeout=self.settings.request_timeout,
            context_window=self.settings.llm_num_ctx or 8192,
        )
        embedding_kwargs: dict[str, Any] = {
            "model_name": self.settings.embedding_model,
            "api_key": self.settings.openai_api_key.get_secret_value(),
            "api_base": self.settings.openai_base_url,
            "timeout": self.settings.embedding_request_timeout,
        }
        if self.settings.embedding_dimensions is not None:
            embedding_kwargs["dimensions"] = self.settings.embedding_dimensions
        LlamaSettings.embed_model = OpenAIEmbedding(**embedding_kwargs)
        LlamaSettings.node_parser = SentenceSplitter(
            chunk_size=self.settings.rag_chunk_size,
            chunk_overlap=self.settings.rag_chunk_overlap,
        )

        vector_store = QdrantVectorStore(
            client=self._client,
            collection_name=self.settings.rag_collection,
        )
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        points_count = int(
            getattr(self._client.get_collection(self.settings.rag_collection), "points_count", 0)
            or 0
        )

        if points_count > 0:
            index = VectorStoreIndex.from_vector_store(vector_store=vector_store)
        else:
            documents = SimpleDirectoryReader(
                input_dir=str(input_dir),
                recursive=True,
            ).load_data()
            index = VectorStoreIndex.from_documents(
                documents,
                storage_context=storage_context,
                show_progress=True,
            )

        self._query_engine = index.as_query_engine(
            similarity_top_k=self.settings.rag_similarity_top_k,
            response_mode="compact",
            text_qa_template=PromptTemplate(QA_PROMPT_TEMPLATE),
        )

    def _answer_sync(self, question: str) -> dict[str, Any]:
        if self._query_engine is None:
            raise RuntimeError("RAGService is not built")

        response = self._query_engine.query(question)
        source_nodes = list(getattr(response, "source_nodes", []) or [])
        top_score = _top_score(source_nodes)
        sources = [
            {
                "text": str(getattr(node, "text", ""))[:300],
                "source": _source_name(node),
                "score": _rounded_score(getattr(node, "score", None)),
            }
            for node in source_nodes
        ]
        answer = str(response)
        if top_score is None or top_score < self.settings.rag_min_top_score:
            answer = UNKNOWN_ANSWER
        return {
            "answer": answer,
            "top_score": _rounded_score(top_score),
            "sources": sources,
        }

    def _input_dir(self) -> Path:
        return self.settings.rag_input_dir


def _top_score(source_nodes: list[Any]) -> float | None:
    scores = [getattr(node, "score", None) for node in source_nodes]
    numeric = [float(score) for score in scores if score is not None]
    if not numeric:
        return None
    return max(numeric)


def _rounded_score(score: float | None) -> float | None:
    if score is None:
        return None
    return round(float(score), 3)


def _source_name(node: Any) -> str | None:
    metadata = getattr(node, "metadata", None) or {}
    for key in ("file_name", "source", "file_path"):
        value = metadata.get(key)
        if value:
            return Path(str(value)).name
    return None


def _collection_vector_size(collection_info: Any) -> int:
    vectors = collection_info.config.params.vectors
    size = getattr(vectors, "size", None)
    if size is not None:
        return int(size)
    if isinstance(vectors, dict):
        first_vector = next(iter(vectors.values()))
        if isinstance(first_vector, dict):
            return int(first_vector["size"])
        return int(first_vector.size)
    raise ValueError("Could not read Qdrant vector size from collection config")


async def _demo() -> None:
    settings = get_settings()
    service = RAGService(settings)
    try:
        await service.build()
        result = await service.answer("Почему в Ansible task лучше избегать command и shell?")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(_demo())
