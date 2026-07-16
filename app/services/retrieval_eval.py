from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RetrievalCase:
    question: str
    relevant_doc_ids: tuple[str, ...]


def load_retrieval_dataset(path: str | Path) -> list[RetrievalCase]:
    dataset_path = Path(path)
    raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{dataset_path} must contain a JSON array")

    cases: list[RetrievalCase] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Dataset item #{index} must be an object")
        question = item.get("question")
        relevant = item.get("relevant_doc_ids")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Dataset item #{index} has an empty question")
        if (
            not isinstance(relevant, list)
            or not 1 <= len(relevant) <= 3
            or not all(isinstance(doc_id, str) and doc_id.strip() for doc_id in relevant)
        ):
            raise ValueError(
                f"Dataset item #{index} relevant_doc_ids must contain 1 to 3 strings"
            )
        cases.append(
            RetrievalCase(
                question=question.strip(),
                relevant_doc_ids=tuple(str(doc_id).strip() for doc_id in relevant),
            )
        )
    if not cases:
        raise ValueError("Retrieval dataset must not be empty")
    return cases


def retrieval_metrics(
    retrieved_doc_ids: Sequence[Sequence[str]],
    relevant_doc_ids: Sequence[Sequence[str]],
) -> dict[str, float]:
    """Calculate macro-averaged Hit Rate@5, MRR@10 and document Recall@10."""

    if len(retrieved_doc_ids) != len(relevant_doc_ids):
        raise ValueError("retrieved_doc_ids and relevant_doc_ids must have the same length")
    if not relevant_doc_ids:
        raise ValueError("At least one evaluation case is required")

    hit_total = 0.0
    reciprocal_rank_total = 0.0
    recall_total = 0.0

    for retrieved, relevant in zip(retrieved_doc_ids, relevant_doc_ids, strict=True):
        relevant_set = {_canonical_doc_id(doc_id) for doc_id in relevant}
        if not relevant_set:
            raise ValueError("Each evaluation case must have at least one relevant document")
        ranked = [_canonical_doc_id(doc_id) for doc_id in retrieved]

        if any(doc_id in relevant_set for doc_id in ranked[:5]):
            hit_total += 1.0

        first_rank = next(
            (rank for rank, doc_id in enumerate(ranked[:10], start=1) if doc_id in relevant_set),
            None,
        )
        if first_rank is not None:
            reciprocal_rank_total += 1.0 / first_rank

        retrieved_unique = set(ranked[:10])
        recall_total += len(retrieved_unique & relevant_set) / len(relevant_set)

    count = len(relevant_doc_ids)
    return {
        "hit_rate@5": hit_total / count,
        "mrr@10": reciprocal_rank_total / count,
        "recall@10": recall_total / count,
    }


def evaluate_retrieval(
    dataset: Sequence[RetrievalCase],
    retrieve: Callable[[str], Sequence[Any]],
) -> dict[str, float]:
    """Run one retriever over a golden dataset and return all required metrics."""

    retrieved_ids: list[list[str]] = []
    relevant_ids: list[Sequence[str]] = []
    for case in dataset:
        candidates = retrieve(case.question)
        retrieved_ids.append([candidate_doc_id(candidate) for candidate in candidates])
        relevant_ids.append(case.relevant_doc_ids)
    return retrieval_metrics(retrieved_ids, relevant_ids)


def candidate_doc_id(candidate: Any) -> str:
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, Mapping):
        return _doc_id_from_mapping(candidate)

    payload = getattr(candidate, "payload", None)
    if isinstance(payload, Mapping):
        return _doc_id_from_mapping(payload)

    metadata = getattr(candidate, "metadata", None)
    if isinstance(metadata, Mapping):
        return _doc_id_from_mapping(metadata)
    raise ValueError(f"Cannot extract document id from {type(candidate).__name__}")


def _doc_id_from_mapping(candidate: Mapping[str, Any]) -> str:
    for key in ("source", "file_name", "file_path", "document_id", "doc_id"):
        value = candidate.get(key)
        if value:
            return str(value)
    raise ValueError("Candidate does not contain a source/document id field")


def _canonical_doc_id(doc_id: str) -> str:
    normalized = str(doc_id).replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", maxsplit=1)[-1]
