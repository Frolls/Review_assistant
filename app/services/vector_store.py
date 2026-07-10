from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from app.core.config import Settings

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http.models import Filter, PointStruct, ScoredPoint


PAYLOAD_INDEXES = (
    ("source", "KEYWORD"),
    ("created_at", "DATETIME"),
    ("tenant_id", "KEYWORD"),
    ("category", "KEYWORD"),
    ("access_level", "KEYWORD"),
    ("archived", "KEYWORD"),
)


class VectorStore:
    def __init__(
        self,
        client: AsyncQdrantClient,
        *,
        collection_name: str,
        embedding_dim: int,
        batch_size: int = 256,
    ) -> None:
        self.client = client
        self.collection_name = collection_name
        self.embedding_dim = embedding_dim
        self.batch_size = batch_size

    async def ensure_collection(self) -> None:
        models = _qdrant_models()
        exists = await _collection_exists(self.client, self.collection_name)
        if exists:
            info = await self.client.get_collection(self.collection_name)
            current_size = _extract_vector_size(info)
            if current_size != self.embedding_dim:
                raise ValueError(
                    f"Qdrant collection {self.collection_name!r} has vector size "
                    f"{current_size}, but EMBEDDING_DIM={self.embedding_dim}."
                )
        else:
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=self.embedding_dim,
                    distance=models.Distance.COSINE,
                ),
                hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
            )

        for field_name, schema_name in PAYLOAD_INDEXES:
            await _create_payload_index(
                self.client,
                collection_name=self.collection_name,
                field_name=field_name,
                field_schema=getattr(models.PayloadSchemaType, schema_name),
            )

    async def upsert(self, points: list[PointStruct], *, batch_size: int | None = None) -> None:
        effective_batch_size = batch_size or self.batch_size
        if effective_batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")

        batches = list(_batched(points, effective_batch_size))
        for index, batch in enumerate(batches):
            await self.client.upsert(
                collection_name=self.collection_name,
                points=batch,
                wait=index == len(batches) - 1,
            )

    async def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        query_filter: Filter | None = None,
    ) -> list[ScoredPoint]:
        if top_k <= 0:
            raise ValueError("top_k must be greater than zero")

        response = await self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=top_k,
        )
        return list(response.points)

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            await close()


def build_vector_store(settings: Settings) -> VectorStore:
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    return VectorStore(
        client,
        collection_name=settings.qdrant_collection,
        embedding_dim=settings.embedding_dim,
    )


def _qdrant_models() -> Any:
    from qdrant_client.http import models

    return models


async def _collection_exists(client: Any, collection_name: str) -> bool:
    collection_exists = getattr(client, "collection_exists", None)
    if collection_exists is not None:
        return bool(await collection_exists(collection_name))

    try:
        await client.get_collection(collection_name)
    except Exception:
        return False
    return True


async def _create_payload_index(
    client: Any,
    *,
    collection_name: str,
    field_name: str,
    field_schema: Any,
) -> None:
    try:
        await client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
        )
    except Exception as exc:
        message = str(exc).lower()
        if "already exists" not in message and "index exists" not in message:
            raise


def _extract_vector_size(collection_info: Any) -> int:
    vectors = collection_info.config.params.vectors
    if isinstance(vectors, dict):
        if "size" in vectors:
            return int(vectors["size"])
        if not vectors:
            raise ValueError("Qdrant collection has no vector configuration")
        first_vector = next(iter(vectors.values()))
        return int(first_vector["size"] if isinstance(first_vector, dict) else first_vector.size)

    size = getattr(vectors, "size", None)
    if size is None:
        raise ValueError("Could not read Qdrant vector size from collection config")
    return int(size)


def _batched(points: Sequence[PointStruct], batch_size: int) -> Iterable[list[PointStruct]]:
    for start in range(0, len(points), batch_size):
        yield list(points[start : start + batch_size])
