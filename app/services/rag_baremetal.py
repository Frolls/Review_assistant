from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from openai import OpenAI

from app.core.config import Settings, get_settings
from app.services.embeddings import EmbeddingConfig, embed_documents, embed_query
from app.services.rag import UNKNOWN_ANSWER


SYSTEM_PROMPT = (
    "/no_think\n"
    "Ты RAG-ассистент для ревью кода. Отвечай только по переданному контексту. "
    "Если в контексте нет ответа, честно скажи, что в корпусе не нашлось информации. "
    "Дай краткий ответ в 3-5 предложениях."
)


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    source: str
    chunk_index: int


class BareMetalRAGService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.embedding_config = EmbeddingConfig.from_settings(settings)
        self.client: Any | None = None

    def build(self, *, force_reindex: bool = False) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http.models import Distance, PointStruct, VectorParams
        except ImportError as exc:  # pragma: no cover - exercised in clean local envs.
            raise RuntimeError("Install qdrant-client to use BareMetalRAGService.") from exc

        self.client = QdrantClient(
            url=self.settings.qdrant_url,
            api_key=self.settings.qdrant_api_key,
        )
        exists = bool(self.client.collection_exists(self.settings.rag_baremetal_collection))
        if exists:
            current_size = _collection_vector_size(
                self.client.get_collection(self.settings.rag_baremetal_collection)
            )
            if current_size != self.settings.embedding_dim:
                raise ValueError(
                    f"Qdrant collection {self.settings.rag_baremetal_collection!r} "
                    f"has vector size {current_size}, but EMBEDDING_DIM={self.settings.embedding_dim}."
                )
            if force_reindex:
                self.client.delete_collection(self.settings.rag_baremetal_collection)
                exists = False

        if not exists:
            self.client.create_collection(
                collection_name=self.settings.rag_baremetal_collection,
                vectors_config=VectorParams(
                    size=self.settings.embedding_dim,
                    distance=Distance.COSINE,
                ),
            )

        info = self.client.get_collection(self.settings.rag_baremetal_collection)
        if int(getattr(info, "points_count", 0) or 0) > 0 and not force_reindex:
            return

        chunks = load_chunks(self.settings.rag_input_dir, self.settings)
        vectors = embed_documents(
            [chunk.text for chunk in chunks],
            config=self.embedding_config,
        )
        bad_dimensions = sorted(
            {len(vector) for vector in vectors if len(vector) != self.settings.embedding_dim}
        )
        if bad_dimensions:
            raise ValueError(
                f"Embedding vector dimensions {bad_dimensions} do not match "
                f"EMBEDDING_DIM={self.settings.embedding_dim}."
            )

        points = [
            PointStruct(
                id=chunk.id,
                vector=vector,
                payload={
                    "text": chunk.text,
                    "source": chunk.source,
                    "file_name": chunk.source,
                    "chunk_index": chunk.chunk_index,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        self.client.upsert(
            collection_name=self.settings.rag_baremetal_collection,
            points=points,
            wait=True,
        )

    def answer(self, question: str) -> dict[str, Any]:
        if self.client is None:
            self.build()
        if self.client is None:
            raise RuntimeError("BareMetalRAGService is not built")

        query_vector = embed_query(question, config=self.embedding_config)
        response = self.client.query_points(
            collection_name=self.settings.rag_baremetal_collection,
            query=query_vector,
            limit=self.settings.rag_similarity_top_k,
        )
        points = list(response.points)
        sources = [
            {
                "text": str(point.payload.get("text", ""))[:300],
                "source": point.payload.get("source"),
                "score": round(float(point.score), 3),
            }
            for point in points
        ]
        top_score = max((float(point.score) for point in points), default=None)
        if top_score is None or top_score < self.settings.rag_min_top_score:
            return {
                "answer": UNKNOWN_ANSWER,
                "top_score": None if top_score is None else round(top_score, 3),
                "sources": sources,
            }

        answer = self._complete(question, points)
        return {
            "answer": answer,
            "top_score": round(top_score, 3),
            "sources": sources,
        }

    def close(self) -> None:
        if self.client is not None:
            close = getattr(self.client, "close", None)
            if close is not None:
                close()

    def _complete(self, question: str, points: list[Any]) -> str:
        context = "\n\n".join(
            f"[{index}. {point.payload.get('source')}] {point.payload.get('text')}"
            for index, point in enumerate(points, start=1)
        )
        client = OpenAI(
            api_key=self.settings.openai_api_key.get_secret_value(),
            base_url=self.settings.openai_base_url,
            timeout=self.settings.request_timeout,
        )
        response = client.chat.completions.create(
            model=self.settings.default_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Контекст:\n{context}\n\nВопрос:\n{question}",
                },
            ],
            temperature=0.1,
            max_tokens=1024,
        )
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        content = getattr(message, "content", "") if message is not None else ""
        return str(content).strip() or UNKNOWN_ANSWER


def load_chunks(input_dir: Path, settings: Settings) -> list[Chunk]:
    if not input_dir.exists():
        raise FileNotFoundError(f"RAG input directory does not exist: {input_dir}")

    chunks: list[Chunk] = []
    for path in sorted(input_dir.rglob("*")):
        if path.is_dir() or path.suffix.lower() not in {".md", ".txt", ".html", ".pdf", ".docx"}:
            continue
        text = _read_text(path)
        for chunk_index, chunk_text in enumerate(
            split_text(
                text,
                chunk_size=settings.rag_chunk_size,
                chunk_overlap=settings.rag_chunk_overlap,
            )
        ):
            chunks.append(
                Chunk(
                    id=str(uuid5(NAMESPACE_URL, f"{path.name}:{chunk_index}:{chunk_text}")),
                    text=chunk_text,
                    source=path.name,
                    chunk_index=chunk_index,
                )
            )
    if not chunks:
        raise RuntimeError(f"No RAG documents found in {input_dir}")
    return chunks


def split_text(text: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    words = re.findall(r"\S+", text)
    if not words:
        return []
    chunks: list[str] = []
    step = max(chunk_size - chunk_overlap, 1)
    for start in range(0, len(words), step):
        chunk_words = words[start : start + chunk_size]
        if not chunk_words:
            break
        chunks.append(" ".join(chunk_words))
        if start + chunk_size >= len(words):
            break
    return chunks


def _read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".html":
        parser = _HTMLTextParser()
        parser.feed(path.read_text(encoding="utf-8"))
        return parser.text
    if suffix == ".pdf":
        from app.chat.media import extract_pdf_text

        return extract_pdf_text(path.read_bytes())
    if suffix == ".docx":
        from app.chat.media import extract_docx_text

        return extract_docx_text(path.read_bytes())
    raise ValueError(f"Unsupported RAG file type: {path}")


class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return " ".join(self._parts)

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)


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


def main() -> None:
    settings = get_settings()
    service = BareMetalRAGService(settings)
    try:
        service.build()
        result = service.answer("Почему в Ansible task лучше избегать command и shell?")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
