from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any


DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 64
DEFAULT_SEMANTIC_BUFFER_SIZE = 1
DEFAULT_BREAKPOINT_PERCENTILE = 95

_RUSSIAN_SENTENCE_BOUNDARY = re.compile(
    r"(?<=[.!?…])\s+(?=(?:[\"«„(]*[A-ZА-ЯЁ№]))"
)


def split_russian_sentences(text: str) -> list[str]:
    """Split Russian/English prose while keeping terminal punctuation on sentences."""

    if not text or not text.strip():
        return []
    return [part for part in _RUSSIAN_SENTENCE_BOUNDARY.split(text) if part.strip()]


def fixed_size(
    documents: Sequence[Any],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Any]:
    """Token-only baseline that does not try to preserve sentence boundaries."""

    _validate_chunk_parameters(chunk_size, chunk_overlap)
    TokenTextSplitter = _node_parser("TokenTextSplitter")
    parser = TokenTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return list(parser.get_nodes_from_documents(list(documents), show_progress=False))


def recursive(
    documents: Sequence[Any],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    chunking_tokenizer_fn: Callable[[str], list[str]] = split_russian_sentences,
) -> list[Any]:
    """Paragraph/sentence-aware recursive splitting for Russian review documents."""

    _validate_chunk_parameters(chunk_size, chunk_overlap)
    SentenceSplitter = _node_parser("SentenceSplitter")
    parser = SentenceSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        paragraph_separator="\n\n",
        chunking_tokenizer_fn=chunking_tokenizer_fn,
    )
    return list(parser.get_nodes_from_documents(list(documents), show_progress=False))


def semantic(
    documents: Sequence[Any],
    *,
    embed_model: Any,
    buffer_size: int = DEFAULT_SEMANTIC_BUFFER_SIZE,
    breakpoint_percentile_threshold: int = DEFAULT_BREAKPOINT_PERCENTILE,
) -> list[Any]:
    """Split on semantic discontinuities using the same embed model as the index."""

    if embed_model is None:
        raise ValueError("embed_model is required for semantic chunking")
    if buffer_size <= 0:
        raise ValueError("buffer_size must be greater than zero")
    if not 0 < breakpoint_percentile_threshold < 100:
        raise ValueError("breakpoint_percentile_threshold must be between 1 and 99")

    SemanticSplitterNodeParser = _node_parser("SemanticSplitterNodeParser")
    parser = SemanticSplitterNodeParser(
        buffer_size=buffer_size,
        breakpoint_percentile_threshold=breakpoint_percentile_threshold,
        embed_model=embed_model,
    )
    return list(parser.get_nodes_from_documents(list(documents), show_progress=False))


def _validate_chunk_parameters(chunk_size: int, chunk_overlap: int) -> None:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must be non-negative")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")


def _node_parser(name: str) -> Any:
    try:
        from llama_index.core import node_parser
    except ImportError as exc:  # pragma: no cover - depends on the deployment extra.
        raise RuntimeError("Install llama-index to use RAG chunking strategies") from exc
    return getattr(node_parser, name)
