from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.services.embeddings import embed_documents, embed_query


DEFAULT_BENCHMARK_PATH = Path("tests/eval/mini_benchmark.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run pairwise embedding retrieval benchmark.")
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=DEFAULT_BENCHMARK_PATH,
        help="Path to JSON benchmark with query/relevant/irrelevant triples.",
    )
    args = parser.parse_args()

    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
    documents = []
    for item in benchmark:
        documents.extend([item["relevant"], item["irrelevant"]])

    started_at = time.perf_counter()
    document_vectors = embed_documents(documents)
    query_vectors = [embed_query(item["query"]) for item in benchmark]
    duration_ms = (time.perf_counter() - started_at) * 1000

    correct = 0
    margins: list[float] = []
    print("idx relevant irrelevant margin verdict")
    for index, (item, query_vector) in enumerate(zip(benchmark, query_vectors, strict=True), start=1):
        relevant_vector = document_vectors[(index - 1) * 2]
        irrelevant_vector = document_vectors[(index - 1) * 2 + 1]
        relevant_score = _dot(query_vector, relevant_vector)
        irrelevant_score = _dot(query_vector, irrelevant_vector)
        margin = relevant_score - irrelevant_score
        margins.append(margin)
        verdict = "ok" if margin > 0 else "fail"
        if verdict == "ok":
            correct += 1
        print(
            f"{index:02d} {relevant_score:.4f} {irrelevant_score:.4f} "
            f"{margin:+.4f} {verdict} :: {item['query']}"
        )

    total = len(benchmark)
    accuracy = correct / total if total else 0.0
    mean_margin = sum(margins) / len(margins) if margins else 0.0
    min_margin = min(margins) if margins else 0.0
    dimensions = len(document_vectors[0]) if document_vectors else 0
    print(
        "\n"
        f"accuracy={correct}/{total} ({accuracy:.1%}) "
        f"mean_margin={mean_margin:+.4f} min_margin={min_margin:+.4f} "
        f"dimensions={dimensions} duration_ms={duration_ms:.2f}"
    )


def _dot(left: list[float], right: list[float]) -> float:
    return sum(left_item * right_item for left_item, right_item in zip(left, right, strict=True))


if __name__ == "__main__":
    main()
