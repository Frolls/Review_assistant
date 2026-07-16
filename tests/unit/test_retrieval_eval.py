from pathlib import Path

import pytest

from app.services.retrieval_eval import (
    RetrievalCase,
    evaluate_retrieval,
    load_retrieval_dataset,
    retrieval_metrics,
)


ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = ROOT / "tests" / "eval" / "retrieval_dataset.json"
CORPUS_PATH = ROOT / "data" / "retrieval-corpus"


def test_golden_dataset_has_20_domain_questions_and_known_sources():
    cases = load_retrieval_dataset(DATASET_PATH)
    corpus_sources = {path.name for path in CORPUS_PATH.glob("*.md")}

    assert len(cases) >= 20
    assert all(1 <= len(case.relevant_doc_ids) <= 3 for case in cases)
    assert len({doc_id for case in cases for doc_id in case.relevant_doc_ids}) >= 8
    assert all(set(case.relevant_doc_ids) <= corpus_sources for case in cases)


def test_retrieval_metrics_use_requested_cutoffs_and_document_recall():
    metrics = retrieval_metrics(
        retrieved_doc_ids=[
            ["wrong.md", "data/right.md", "right.md"],
            ["wrong.md"] * 5 + ["second.md", "first.md"],
        ],
        relevant_doc_ids=[
            ["right.md", "also_right.md"],
            ["first.md", "second.md"],
        ],
    )

    assert metrics["hit_rate@5"] == pytest.approx(0.5)
    assert metrics["mrr@10"] == pytest.approx((1 / 2 + 1 / 6) / 2)
    assert metrics["recall@10"] == pytest.approx(0.75)


def test_evaluate_retrieval_returns_all_three_metrics():
    dataset = [RetrievalCase("question", ("source.md",))]

    metrics = evaluate_retrieval(
        dataset,
        lambda _: [{"source": "source.md", "text": "relevant chunk"}],
    )

    assert metrics == {"hit_rate@5": 1.0, "mrr@10": 1.0, "recall@10": 1.0}
