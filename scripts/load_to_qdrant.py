from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import Distance, HnswConfigDiff, PointStruct, VectorParams
from tqdm import tqdm

from app.core.config import get_settings
from app.services.embeddings import embed_documents, embed_query
from app.services.vector_store import VectorStore


DEFAULT_DATA_DIR = Path("data")
REQUIRED_DOCUMENT_FIELDS = {
    "source",
    "category",
    "department",
    "tenant_id",
    "access_level",
    "archived",
    "created_at",
    "chunks",
}
METRIC_QUERIES = [
    "Как проверить безопасность PR с внешними URL и токенами?",
    "Какие правила использовать для Ansible idempotency?",
    "Что делать с flaky тестами в pull request?",
    "Как ревьюить миграцию Postgres без долгих блокировок?",
    "Какие документы доступны только platform team?",
]


@dataclass(frozen=True)
class DocumentChunk:
    id: str
    text: str
    payload: dict[str, object]


def main() -> None:
    parser = argparse.ArgumentParser(description="Load review knowledge chunks into Qdrant.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument(
        "--compare-metrics",
        action="store_true",
        help="Also compare COSINE and DOT ranking on five project queries, then delete temp collections.",
    )
    args = parser.parse_args()
    asyncio.run(load(args.data_dir, batch_size=args.batch_size, compare_metrics=args.compare_metrics))


async def load(data_dir: Path, *, batch_size: int, compare_metrics: bool) -> None:
    settings = get_settings()
    client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    vector_store = VectorStore(
        client,
        collection_name=settings.qdrant_collection,
        embedding_dim=settings.embedding_dim,
        batch_size=batch_size,
    )
    try:
        await vector_store.ensure_collection()
        chunks = load_chunks(data_dir)
        if len(chunks) < 100:
            raise RuntimeError(f"Expected at least 100 chunks from {data_dir}, got {len(chunks)}")

        vectors: list[list[float]] = []
        for start in tqdm(range(0, len(chunks), batch_size), desc="embedding", unit="batch"):
            batch = chunks[start : start + batch_size]
            vectors.extend(embed_documents([chunk.text for chunk in batch]))

        bad_dimensions = sorted({len(vector) for vector in vectors if len(vector) != settings.embedding_dim})
        if bad_dimensions:
            raise ValueError(
                f"Embedding vector dimensions {bad_dimensions} do not match "
                f"EMBEDDING_DIM={settings.embedding_dim}."
            )

        points = [
            PointStruct(id=chunk.id, vector=vector, payload=chunk.payload)
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        point_batches = [
            points[start : start + batch_size] for start in range(0, len(points), batch_size)
        ]
        for batch in tqdm(point_batches, desc="upsert", unit="batch"):
            await vector_store.upsert(batch, batch_size=batch_size)
        info = await client.get_collection(settings.qdrant_collection)
        print(f"Loaded {len(points)} chunks into {settings.qdrant_collection}.")
        print(f"points_count={info.points_count}")

        if compare_metrics:
            await compare_distance_metrics(client, points, settings.embedding_dim)
    finally:
        await client.close()


def load_chunks(data_dir: Path) -> list[DocumentChunk]:
    if not data_dir.exists():
        raise FileNotFoundError(f"Data directory does not exist: {data_dir}")

    chunks: list[DocumentChunk] = []
    json_paths = sorted(data_dir.glob("*.json"))
    if json_paths:
        for path in json_paths:
            chunks.extend(_load_json_chunks(path))
    else:
        for path in sorted(data_dir.glob("*.md")):
            chunks.extend(_load_markdown_chunks(path))

    if not chunks:
        raise RuntimeError(f"No knowledge chunks found in {data_dir}")
    return chunks


def _load_json_chunks(path: Path) -> list[DocumentChunk]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    documents = _read_documents(raw, path)

    chunks: list[DocumentChunk] = []
    for document_index, document in enumerate(documents):
        metadata, raw_chunks = _validate_document(document, document_index, path)
        for chunk_index, text in enumerate(raw_chunks):
            document_id = f"{metadata['source']}#{chunk_index}"
            payload = {
                **metadata,
                "document_id": document_id,
                "text": text,
                "chunk_index": chunk_index,
            }
            chunks.append(
                DocumentChunk(
                    id=_point_id(metadata["source"], metadata["category"], chunk_index, text),
                    text=text,
                    payload=payload,
                )
            )
    return chunks


def _read_documents(raw: Any, path: Path) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw_documents = raw.get("documents")
    else:
        raw_documents = raw

    if not isinstance(raw_documents, list):
        raise ValueError(f"{path} must contain a JSON array or an object with documents array")
    if not all(isinstance(document, dict) for document in raw_documents):
        raise ValueError(f"{path} documents must be JSON objects")
    return raw_documents


def _validate_document(
    document: dict[str, Any],
    document_index: int,
    path: Path,
) -> tuple[dict[str, str], list[str]]:
    missing = REQUIRED_DOCUMENT_FIELDS - document.keys()
    if missing:
        fields = ", ".join(sorted(missing))
        raise ValueError(f"{path} document #{document_index} is missing fields: {fields}")

    chunks = document["chunks"]
    if not isinstance(chunks, list) or not chunks:
        raise ValueError(f"{path} document #{document_index} must contain a non-empty chunks list")
    if not all(isinstance(chunk, str) and chunk.strip() for chunk in chunks):
        raise ValueError(f"{path} document #{document_index} chunks must be non-empty strings")

    source = _required_string(document, "source", document_index, path)
    category = _required_string(document, "category", document_index, path)
    metadata = {
        "source": source,
        "title": _optional_string(document, "title") or source,
        "category": category,
        "department": _required_string(document, "department", document_index, path),
        "tenant_id": _required_string(document, "tenant_id", document_index, path),
        "access_level": _required_string(document, "access_level", document_index, path),
        "archived": _archived_value(document["archived"], document_index, path),
        "created_at": _as_datetime(_required_string(document, "created_at", document_index, path)),
    }
    return metadata, [chunk.strip() for chunk in chunks]


def _required_string(
    document: dict[str, Any],
    field: str,
    document_index: int,
    path: Path,
) -> str:
    value = document[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} document #{document_index} field {field!r} must be a string")
    return value.strip()


def _optional_string(document: dict[str, Any], field: str) -> str | None:
    value = document.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Optional field {field!r} must be a string when provided")
    return value.strip() or None


def _archived_value(value: Any, document_index: int, path: Path) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower()
    raise ValueError(f"{path} document #{document_index} field 'archived' must be boolean")


def _load_markdown_chunks(path: Path) -> list[DocumentChunk]:
    chunks: list[DocumentChunk] = []
    metadata: dict[str, str] | None = None
    chunk_index = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            metadata = _parse_heading(stripped[3:], path)
            chunk_index = 0
            continue
        if not stripped.startswith("- "):
            continue
        if metadata is None:
            raise ValueError(f"Chunk before metadata heading in {path}")

        text = stripped[2:].strip()
        document_id = f"{metadata['source']}#{chunk_index}"
        payload = {
            **metadata,
            "document_id": document_id,
            "text": text,
            "chunk_index": chunk_index,
        }
        point_id = _point_id(metadata["source"], metadata["category"], chunk_index, text)
        chunks.append(DocumentChunk(id=point_id, text=text, payload=payload))
        chunk_index += 1
    return chunks


def _parse_heading(raw_heading: str, path: Path) -> dict[str, str]:
    parts = [part.strip() for part in raw_heading.split("|")]
    if len(parts) != 7:
        raise ValueError(
            f"Expected heading format in {path}: "
            "source | category | department | tenant_id | access_level | archived | created_at"
        )
    source, category, department, tenant_id, access_level, archived, created_at = parts
    return {
        "source": source,
        "category": category,
        "department": department,
        "tenant_id": tenant_id,
        "access_level": access_level,
        "archived": archived.lower(),
        "created_at": _as_datetime(created_at),
    }


def _as_datetime(value: str) -> str:
    if "T" in value:
        return value
    return f"{value}T00:00:00Z"


def _point_id(source: str, category: str, chunk_index: int, text: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{source}:{category}:{chunk_index}:{text}"))


async def compare_distance_metrics(
    client: AsyncQdrantClient,
    points: list[PointStruct],
    embedding_dim: int,
) -> None:
    cosine_collection = "documents_cosine"
    dot_collection = "documents_dot"
    for collection_name, distance in (
        (cosine_collection, Distance.COSINE),
        (dot_collection, Distance.DOT),
    ):
        if await client.collection_exists(collection_name):
            await client.delete_collection(collection_name)
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=embedding_dim, distance=distance),
            hnsw_config=HnswConfigDiff(m=16, ef_construct=100),
        )
        await client.upsert(collection_name=collection_name, points=points, wait=True)

    print("\n| query | cosine top-5 ids | dot top-5 ids | same ranking |")
    print("| --- | --- | --- | --- |")
    for query in METRIC_QUERIES:
        query_vector = embed_query(query)
        cosine = await client.query_points(
            collection_name=cosine_collection,
            query=query_vector,
            limit=5,
        )
        dot = await client.query_points(collection_name=dot_collection, query=query_vector, limit=5)
        cosine_ids = [_display_point_id(point) for point in cosine.points]
        dot_ids = [_display_point_id(point) for point in dot.points]
        print(
            f"| {query} | `{', '.join(cosine_ids)}` | "
            f"`{', '.join(dot_ids)}` | {cosine_ids == dot_ids} |"
        )

    await client.delete_collection(dot_collection)
    await client.delete_collection(cosine_collection)


def _display_point_id(point: object) -> str:
    payload = getattr(point, "payload", None) or {}
    document_id = payload.get("document_id") if isinstance(payload, dict) else None
    return str(document_id or getattr(point, "id"))


if __name__ == "__main__":
    main()
