from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import vector_store
from app.services.vector_store import VectorStore


class FakeVectorParams:
    def __init__(self, *, size: int, distance: str) -> None:
        self.size = size
        self.distance = distance


class FakeHnswConfigDiff:
    def __init__(self, *, m: int, ef_construct: int) -> None:
        self.m = m
        self.ef_construct = ef_construct


class FakeClient:
    def __init__(self, *, exists: bool = False, vector_size: int = 2560) -> None:
        self.exists = exists
        self.vector_size = vector_size
        self.created_collections: list[dict] = []
        self.created_indexes: list[tuple[str, str]] = []
        self.upserts: list[dict] = []
        self.closed = False

    async def collection_exists(self, collection_name: str) -> bool:
        return self.exists

    async def get_collection(self, collection_name: str):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=self.vector_size))
            )
        )

    async def create_collection(self, **kwargs) -> None:
        self.created_collections.append(kwargs)
        self.exists = True

    async def create_payload_index(self, **kwargs) -> None:
        self.created_indexes.append((kwargs["field_name"], kwargs["field_schema"]))

    async def upsert(self, **kwargs) -> None:
        self.upserts.append(kwargs)

    async def query_points(self, **kwargs):
        return SimpleNamespace(points=[SimpleNamespace(id="point-1", score=0.9)])

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_qdrant_models(monkeypatch):
    monkeypatch.setattr(
        vector_store,
        "_qdrant_models",
        lambda: SimpleNamespace(
            Distance=SimpleNamespace(COSINE="cosine"),
            HnswConfigDiff=FakeHnswConfigDiff,
            PayloadSchemaType=SimpleNamespace(
                KEYWORD="keyword",
                DATETIME="datetime",
            ),
            VectorParams=FakeVectorParams,
        ),
    )


@pytest.mark.asyncio
async def test_ensure_collection_creates_collection_and_payload_indexes():
    client = FakeClient(exists=False)
    store = VectorStore(client, collection_name="documents", embedding_dim=2560)

    await store.ensure_collection()

    assert client.created_collections[0]["collection_name"] == "documents"
    assert client.created_collections[0]["vectors_config"].size == 2560
    assert client.created_collections[0]["vectors_config"].distance == "cosine"
    assert client.created_collections[0]["hnsw_config"].m == 16
    assert ("source", "keyword") in client.created_indexes
    assert ("created_at", "datetime") in client.created_indexes
    assert ("tenant_id", "keyword") in client.created_indexes
    assert ("archived", "keyword") in client.created_indexes


@pytest.mark.asyncio
async def test_ensure_collection_rejects_wrong_vector_size():
    client = FakeClient(exists=True, vector_size=1536)
    store = VectorStore(client, collection_name="documents", embedding_dim=2560)

    with pytest.raises(ValueError, match="EMBEDDING_DIM=2560"):
        await store.ensure_collection()


@pytest.mark.asyncio
async def test_upsert_batches_and_search_returns_points():
    client = FakeClient(exists=True)
    store = VectorStore(client, collection_name="documents", embedding_dim=2560, batch_size=2)

    await store.upsert([{"id": "1"}, {"id": "2"}, {"id": "3"}])
    points = await store.search([0.1, 0.2], top_k=1)
    await store.close()

    assert [len(call["points"]) for call in client.upserts] == [2, 1]
    assert [call["wait"] for call in client.upserts] == [False, True]
    assert points[0].id == "point-1"
    assert client.closed is True
