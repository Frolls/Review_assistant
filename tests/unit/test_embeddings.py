from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import embeddings


MINI_BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "eval" / "mini_benchmark.json"


class FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        data = [
            SimpleNamespace(index=index, embedding=[float(index + 1), 0.0, 0.0])
            for index, _ in enumerate(kwargs["input"])
        ]
        return SimpleNamespace(data=data)


class FakeOpenAI:
    api = FakeEmbeddingsAPI()

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.embeddings = self.api


@pytest.fixture(autouse=True)
def embedding_env(monkeypatch, tmp_path):
    FakeOpenAI.api = FakeEmbeddingsAPI()
    monkeypatch.setattr(embeddings, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("EMBEDDING_CACHE_PATH", str(tmp_path / "embeddings.sqlite"))
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)


def test_embed_texts_batches_and_caches_repeated_values():
    vectors = embeddings.embed_texts(["one", "two", "three", "one"])
    cached_vectors = embeddings.embed_texts(["one"])

    assert len(vectors) == 4
    assert cached_vectors == [vectors[0]]
    assert [call["input"] for call in FakeOpenAI.api.calls] == [["one", "two"], ["three"]]


def test_cache_key_changes_when_model_changes(monkeypatch):
    embeddings.embed_texts(["same text"])
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-large")
    embeddings.embed_texts(["same text"])

    assert len(FakeOpenAI.api.calls) == 2
    assert FakeOpenAI.api.calls[0]["model"] == "text-embedding-3-small"
    assert FakeOpenAI.api.calls[1]["model"] == "text-embedding-3-large"


def test_e5_query_and_document_prefixes_are_applied(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")

    embeddings.embed_query("как устроен rate limit")
    embeddings.embed_documents(["HTTP limiter использует Redis"])

    assert FakeOpenAI.api.calls[0]["input"] == ["query: как устроен rate limit"]
    assert FakeOpenAI.api.calls[1]["input"] == ["passage: HTTP limiter использует Redis"]


def test_qwen3_embedding_query_instruction_is_applied(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "qwen3-embedding:4b")

    embeddings.embed_query("как устроен rate limit")
    embeddings.embed_documents(["HTTP limiter использует Redis"])

    query_input = FakeOpenAI.api.calls[0]["input"][0]
    assert query_input.startswith("Instruct: For a Russian or English question")
    assert query_input.endswith("Query: как устроен rate limit")
    assert FakeOpenAI.api.calls[1]["input"] == ["HTTP limiter использует Redis"]


def test_mini_benchmark_has_required_shape():
    benchmark = json.loads(MINI_BENCHMARK_PATH.read_text(encoding="utf-8"))

    assert 5 <= len(benchmark) <= 10
    for item in benchmark:
        assert set(item) == {"query", "relevant", "irrelevant"}
        assert all(isinstance(value, str) and value.strip() for value in item.values())
