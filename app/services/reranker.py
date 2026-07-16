from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class BGEReranker:
    """Lazy sentence-transformers wrapper around the multilingual BGE cross-encoder."""

    def __init__(
        self,
        model_name: str = DEFAULT_RERANKER_MODEL,
        *,
        model: Any | None = None,
        device: str | None = None,
        max_length: int = 1024,
    ) -> None:
        if not model_name.strip():
            raise ValueError("model_name must not be blank")
        if max_length <= 0:
            raise ValueError("max_length must be greater than zero")
        self.model_name = model_name
        self.device = device
        self.max_length = max_length
        self._model = model

    def rerank(
        self,
        query: str,
        candidates: Sequence[Any],
        *,
        top_n: int = 10,
    ) -> list[Any]:
        if not query.strip():
            raise ValueError("query must not be blank")
        if top_n <= 0:
            raise ValueError("top_n must be greater than zero")
        if not candidates:
            return []

        model = self._get_model()
        pairs = [(query, candidate_text(candidate)) for candidate in candidates]
        raw_scores = model.predict(
            pairs,
            batch_size=min(32, len(pairs)),
            show_progress_bar=False,
        )
        scores = [float(score) for score in raw_scores]
        if len(scores) != len(candidates):
            raise RuntimeError("Reranker returned an unexpected number of scores")

        ranked = sorted(
            enumerate(zip(candidates, scores, strict=True)),
            key=lambda item: (-item[1][1], item[0]),
        )
        return [candidate for _, (candidate, _) in ranked[:top_n]]

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - optional heavyweight dependency.
            raise RuntimeError(
                "Install the local-embeddings extra to use BAAI/bge-reranker-v2-m3"
            ) from exc

        kwargs: dict[str, Any] = {
            "trust_remote_code": True,
            "max_length": self.max_length,
        }
        if self.device is not None:
            kwargs["device"] = self.device
        self._model = CrossEncoder(self.model_name, **kwargs)
        return self._model


def candidate_text(candidate: Any) -> str:
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, Mapping):
        return _text_from_mapping(candidate)

    payload = getattr(candidate, "payload", None)
    if isinstance(payload, Mapping):
        return _text_from_mapping(payload)

    text = getattr(candidate, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    get_content = getattr(candidate, "get_content", None)
    if callable(get_content):
        content = get_content()
        if isinstance(content, str) and content.strip():
            return content
    raise ValueError(f"Cannot extract text from {type(candidate).__name__}")


def _text_from_mapping(candidate: Mapping[str, Any]) -> str:
    for key in ("text", "content", "page_content"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise ValueError("Candidate does not contain a text field")
