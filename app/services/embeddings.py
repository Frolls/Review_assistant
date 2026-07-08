from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openai import (
    APIConnectionError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)


logger = logging.getLogger(__name__)

Provider = Literal["openai", "sentence-transformers"]
InputType = Literal["text", "query", "document"]
DEFAULT_OPENAI_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_BATCH_SIZE = 128
DEFAULT_SENTENCE_TRANSFORMERS_BATCH_SIZE = 32
QWEN3_QUERY_INSTRUCTION = (
    "Instruct: For a Russian or English question about a PR review assistant, "
    "retrieve the most relevant Russian or English documentation passage.\n"
    "Query: "
)
RETRYABLE_OPENAI_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: Provider
    model: str
    batch_size: int
    cache_path: Path
    dimensions: int | None
    request_timeout: float
    api_key: str | None
    base_url: str | None

    @classmethod
    def from_env(cls) -> EmbeddingConfig:
        model = os.getenv("EMBEDDING_MODEL", DEFAULT_OPENAI_MODEL).strip()
        provider = _resolve_provider(os.getenv("EMBEDDING_PROVIDER"), model)
        default_batch_size = (
            DEFAULT_SENTENCE_TRANSFORMERS_BATCH_SIZE
            if provider == "sentence-transformers"
            else DEFAULT_OPENAI_BATCH_SIZE
        )
        batch_size = _env_int("EMBEDDING_BATCH_SIZE", default_batch_size)
        if batch_size < 1:
            raise ValueError("EMBEDDING_BATCH_SIZE must be greater than zero")

        dimensions = _env_optional_int("EMBEDDING_DIMENSIONS")
        if dimensions is not None and dimensions < 1:
            raise ValueError("EMBEDDING_DIMENSIONS must be greater than zero")

        cache_path = Path(os.getenv("EMBEDDING_CACHE_PATH", ".cache/embeddings.sqlite"))
        return cls(
            provider=provider,
            model=model,
            batch_size=batch_size,
            cache_path=cache_path,
            dimensions=dimensions,
            request_timeout=float(os.getenv("EMBEDDING_REQUEST_TIMEOUT", "30")),
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=_blank_to_none(os.getenv("OPENAI_BASE_URL")),
        )


class EmbeddingCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                key TEXT PRIMARY KEY,
                vector_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )

    def get(self, key: str) -> list[float] | None:
        row = self._connection.execute(
            "SELECT vector_json FROM embeddings WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def set(self, key: str, vector: list[float]) -> None:
        self._connection.execute(
            """
            INSERT INTO embeddings (key, vector_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                vector_json = excluded.vector_json,
                created_at = excluded.created_at
            """,
            (key, json.dumps(vector, separators=(",", ":")), time.time()),
        )
        self._connection.commit()


_sentence_transformer_model: Any | None = None
_sentence_transformer_model_name: str | None = None


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed arbitrary text snippets using the configured model."""

    return _embed_texts(texts, input_type="text")


def embed_query(text: str) -> list[float]:
    """Embed a search query with model-specific asymmetric retrieval prefixes."""

    return _embed_texts([text], input_type="query")[0]


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed searchable passages with model-specific asymmetric retrieval prefixes."""

    return _embed_texts(texts, input_type="document")


def _embed_texts(texts: list[str], *, input_type: InputType) -> list[list[float]]:
    if not isinstance(texts, list):
        raise TypeError("texts must be a list[str]")
    if not texts:
        return []
    if any(not isinstance(text, str) for text in texts):
        raise TypeError("all texts must be strings")

    config = EmbeddingConfig.from_env()
    cache = EmbeddingCache(config.cache_path)
    cache_keys = [_cache_key(config, text, input_type=input_type) for text in texts]
    results: list[list[float] | None] = [None] * len(texts)
    misses: dict[str, str] = {}

    for index, (text, key) in enumerate(zip(texts, cache_keys, strict=True)):
        cached = cache.get(key)
        if cached is not None:
            results[index] = cached
            continue
        misses.setdefault(key, text)

    if misses:
        logger.info(
            "embedding.cache_miss",
            extra={
                "provider": config.provider,
                "model": config.model,
                "misses": len(misses),
            },
        )
        for keys_batch in _batched(list(misses), config.batch_size):
            raw_texts = [misses[key] for key in keys_batch]
            vectors = _embed_uncached(config, raw_texts, input_type=input_type)
            for key, vector in zip(keys_batch, vectors, strict=True):
                cache.set(key, vector)

    for index, key in enumerate(cache_keys):
        if results[index] is None:
            results[index] = cache.get(key)
        if results[index] is None:
            raise RuntimeError(f"embedding cache did not persist vector for key {key}")

    return [vector for vector in results if vector is not None]


def _embed_uncached(
    config: EmbeddingConfig,
    texts: Sequence[str],
    *,
    input_type: InputType,
) -> list[list[float]]:
    prepared = [_prepare_text(config.model, text, input_type=input_type) for text in texts]
    if config.provider == "sentence-transformers":
        return _embed_with_sentence_transformers(config, prepared)
    return _embed_with_openai(config, prepared)


def _embed_with_openai(config: EmbeddingConfig, texts: Sequence[str]) -> list[list[float]]:
    if not config.api_key:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI embeddings")

    client = OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.request_timeout,
    )
    payload: dict[str, Any] = {"model": config.model, "input": list(texts)}
    if config.dimensions is not None:
        payload["dimensions"] = config.dimensions

    response = _call_openai_with_retry(client, payload)
    vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
    return [_normalize(vector) for vector in vectors]


def _call_openai_with_retry(client: OpenAI, payload: dict[str, Any]) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            return client.embeddings.create(**payload)
        except RETRYABLE_OPENAI_ERRORS as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(2 ** (attempt - 1))
        except OpenAIError:
            raise
    raise RuntimeError("OpenAI embeddings request failed after retries") from last_error


def _embed_with_sentence_transformers(
    config: EmbeddingConfig,
    texts: Sequence[str],
) -> list[list[float]]:
    model = _get_sentence_transformer(config.model)
    embeddings = model.encode(
        list(texts),
        batch_size=config.batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return [_to_float_list(vector) for vector in embeddings]


def _get_sentence_transformer(model_name: str) -> Any:
    global _sentence_transformer_model, _sentence_transformer_model_name

    if _sentence_transformer_model is not None and _sentence_transformer_model_name == model_name:
        return _sentence_transformer_model

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "Install sentence-transformers to use EMBEDDING_PROVIDER=sentence-transformers"
        ) from exc

    _sentence_transformer_model = SentenceTransformer(model_name)
    _sentence_transformer_model_name = model_name
    return _sentence_transformer_model


def _prepare_text(model: str, text: str, *, input_type: InputType) -> str:
    if _is_qwen3_embedding_model(model):
        if input_type == "query":
            return f"{QWEN3_QUERY_INSTRUCTION}{text}"
        return text
    if not _is_e5_model(model):
        return text
    if input_type == "query":
        return f"query: {text}"
    if input_type == "document":
        return f"passage: {text}"
    return text


def _cache_key(config: EmbeddingConfig, text: str, *, input_type: InputType) -> str:
    payload = {
        "version": 1,
        "provider": config.provider,
        "model": config.model,
        "dimensions": config.dimensions,
        "input_type": input_type,
        "text": text,
        "e5_prefixing": _is_e5_model(config.model),
        "qwen3_instruction": _is_qwen3_embedding_model(config.model),
    }
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _batched(items: Sequence[str], batch_size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), batch_size):
        yield list(items[start : start + batch_size])


def _normalize(vector: Sequence[float]) -> list[float]:
    values = [float(item) for item in vector]
    norm = math.sqrt(sum(item * item for item in values))
    if norm == 0:
        return values
    return [item / norm for item in values]


def _to_float_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        return [float(item) for item in vector.tolist()]
    return [float(item) for item in vector]


def _resolve_provider(raw_provider: str | None, model: str) -> Provider:
    provider = (raw_provider or "auto").strip().lower()
    if provider == "auto":
        return "sentence-transformers" if "/" in model else "openai"
    if provider in {"sentence-transformers", "sentence_transformers", "local"}:
        return "sentence-transformers"
    if provider == "openai":
        return "openai"
    raise ValueError("EMBEDDING_PROVIDER must be openai, sentence-transformers, or auto")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return int(value)


def _blank_to_none(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value


def _is_e5_model(model: str) -> bool:
    model_parts = model.lower().replace("_", "-").split("/")
    return any(part.startswith("e5-") or "-e5-" in part or part == "e5" for part in model_parts)


def _is_qwen3_embedding_model(model: str) -> bool:
    normalized = model.lower().replace("_", "-")
    return "qwen3-embedding" in normalized
