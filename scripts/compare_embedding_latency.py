from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.core.config import get_settings
from app.services.embeddings import EmbeddingConfig, embed_query
from app.services.retrieval_eval import load_retrieval_dataset


DEFAULT_MODELS = ("qwen3-embedding:4b", "qwen3-embedding:0.6b")


@dataclass(frozen=True)
class LatencyResult:
    model: str
    embedding_dimension: int
    mean_ms: float
    median_ms: float
    p95_ms: float


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare uncached query-embedding latency after model warm-up."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/eval/retrieval_dataset.json"),
    )
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    args = parser.parse_args()

    cases = load_retrieval_dataset(args.dataset)
    base_config = EmbeddingConfig.from_settings(get_settings())
    results: list[LatencyResult] = []
    with tempfile.TemporaryDirectory(prefix="embedding-latency-") as temp_dir:
        for index, model in enumerate(args.models):
            config = replace(
                base_config,
                model=model,
                cache_path=Path(temp_dir) / f"model-{index}.sqlite",
                dimensions=None,
            )
            embed_query("warm-up query", config=config)
            latencies: list[float] = []
            dimension = 0
            for case in cases:
                started = time.perf_counter()
                vector = embed_query(case.question, config=config)
                latencies.append((time.perf_counter() - started) * 1000)
                dimension = len(vector)
            results.append(
                LatencyResult(
                    model=model,
                    embedding_dimension=dimension,
                    mean_ms=statistics.fmean(latencies),
                    median_ms=statistics.median(latencies),
                    p95_ms=statistics.quantiles(
                        latencies,
                        n=20,
                        method="inclusive",
                    )[18],
                )
            )

    print(json.dumps([asdict(result) for result in results], indent=2))


if __name__ == "__main__":
    main()
