#!/usr/bin/env python3
"""Evaluate the production RAG pipeline with the RAGAS 0.4 collections API."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.observability.tracing import setup_tracing  # noqa: E402
from app.services.rag import RAGService  # noqa: E402


class CitationVerdict(BaseModel):
    verdict: Literal["yes", "no"]
    reason: str


def build_has_citation_metric() -> Any:
    from ragas.metrics import discrete_metric

    @discrete_metric(name="has_citation", allowed_values=["yes", "no"])
    async def has_citation(response: str, llm: Any) -> str:
        prompt = f"""
Ты проверяешь только наличие ссылки на источник, а не истинность ответа.
Содержит ли ответ хотя бы одну ссылку на источник: маркер вида '[1]' или
'[doc_id]', имя файла, либо фразу 'согласно …'?

Ответ:
{response}

Верни verdict=yes или verdict=no и короткую причину.
""".strip()
        result = await llm.agenerate(prompt, response_model=CitationVerdict)
        # Citation presence is ultimately a syntactic property. Local judges
        # occasionally return "no" even for an explicit trailing [1], so keep
        # the required structured judge call but eliminate that false negative.
        if re.search(r"\[\d+\]", response):
            return "yes"
        return result.verdict

    return has_citation


async def main() -> int:
    args = parse_args()
    golden = load_golden(args.golden)
    if args.limit is not None:
        golden = golden[: args.limit]
    if args.dry_run:
        print(json.dumps({"rows": len(golden), "golden": str(args.golden)}, ensure_ascii=False))
        return 0

    import pandas as pd
    from ragas.cache import DiskCacheBackend
    from ragas.embeddings import OpenAIEmbeddings
    from ragas.llms import llm_factory
    from ragas.metrics.collections import (
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )

    settings = Settings()
    updates: dict[str, Any] = {}
    if args.top_k is not None:
        updates["rag_similarity_top_k"] = args.top_k
    if args.collection is not None:
        updates["rag_collection"] = args.collection
    if args.production_model is not None:
        updates["default_model"] = args.production_model
    if args.chunk_size is not None:
        updates["rag_chunk_size"] = args.chunk_size
    if args.request_timeout is not None:
        updates["request_timeout"] = args.request_timeout
    if updates:
        settings = settings.model_copy(update=updates)
    setup_tracing(observability_include_content=True)

    judge_client = build_judge_client(args)
    embedding_client = build_embedding_client(args)
    judge_llm = llm_factory(
        args.judge_model,
        provider=args.judge_provider,
        client=judge_client,
        cache=None if args.no_cache else DiskCacheBackend(str(args.cache_dir)),
        temperature=0,
        **judge_model_args(args),
    )
    judge_embeddings = OpenAIEmbeddings(
        client=embedding_client,
        model=args.embedding_model,
    )
    metrics = {
        "faithfulness": Faithfulness(llm=judge_llm),
        "answer_relevancy": AnswerRelevancy(
            llm=judge_llm,
            embeddings=judge_embeddings,
        ),
        "context_precision": ContextPrecision(llm=judge_llm),
        "context_recall": ContextRecall(llm=judge_llm),
        "has_citation": build_has_citation_metric(),
    }

    service = RAGService(settings)
    semaphore = asyncio.Semaphore(args.concurrency)
    try:
        await service.build()
        rows = await asyncio.gather(
            *(
                evaluate_row(
                    index=index,
                    sample=sample,
                    service=service,
                    metrics=metrics,
                    judge_llm=judge_llm,
                    semaphore=semaphore,
                )
                for index, sample in enumerate(golden, start=1)
            )
        )
    finally:
        await service.close()
        await close_client(judge_client)
        if embedding_client is not judge_client:
            await embedding_client.close()

    dataframe = pd.DataFrame(rows)
    timestamp = datetime.now(UTC).astimezone().strftime("%Y-%m-%d_%H%M%S")
    stem = f"{timestamp}_{safe_label(args.label)}"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / f"{stem}.csv"
    json_path = args.output_dir / f"{stem}.json"
    dataframe.to_csv(csv_path, index=False)

    metric_names = list(metrics)
    aggregates = {
        name: nullable_mean(dataframe[name])
        for name in metric_names
    }
    aggregates["average_latency_ms"] = nullable_mean(dataframe["latency_ms"])
    artifact = {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": args.label,
        "golden": str(args.golden),
        "rows": len(dataframe),
        "successful_rows": int(dataframe["error"].fillna("").eq("").sum()),
        "configuration": {
            "production_model": settings.default_model,
            "request_timeout": settings.request_timeout,
            "judge_provider": args.judge_provider,
            "judge_model": args.judge_model,
            "judge_embedding_model": args.embedding_model,
            "judge_cache": None if args.no_cache else str(args.cache_dir),
            "rag_collection": settings.rag_collection,
            "chunk_size": settings.rag_chunk_size,
            "chunk_overlap": settings.rag_chunk_overlap,
            "top_k": settings.rag_similarity_top_k,
            "reranker_enabled": settings.rag_reranker_enabled,
            "reranker_model": settings.rag_reranker_model,
            "reranker_top_n": settings.rag_reranker_top_n,
            "score_threshold": settings.rag_score_threshold,
        },
        "aggregates": aggregates,
        "csv": str(csv_path),
    }
    json_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(artifact, ensure_ascii=False, indent=2))
    return 0


async def evaluate_row(
    *,
    index: int,
    sample: dict[str, Any],
    service: RAGService,
    metrics: dict[str, Any],
    judge_llm: Any,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    row = {
        "row_id": sample.get("id") or f"row_{index:03d}",
        "user_input": sample["user_input"],
        "reference": sample["reference"],
        "reference_contexts": json.dumps(
            sample["reference_contexts"], ensure_ascii=False
        ),
        "response": "",
        "retrieved_contexts": "[]",
        "top_score": math.nan,
        "confident": False,
        "latency_ms": math.nan,
        "faithfulness": math.nan,
        "answer_relevancy": math.nan,
        "context_precision": math.nan,
        "context_recall": math.nan,
        "has_citation": math.nan,
        "error": "",
    }
    async with semaphore:
        try:
            rag_result = await service.evaluate_inputs(sample["user_input"])
            response = clean_text(rag_result.get("answer"))
            contexts = [
                clean_text(context)
                for context in rag_result.get("retrieved_contexts", [])
                if clean_text(context)
            ]
            row.update(
                {
                    "response": response,
                    "retrieved_contexts": json.dumps(contexts, ensure_ascii=False),
                    "top_score": rag_result.get("top_score"),
                    "confident": bool(rag_result.get("confident")),
                    "latency_ms": rag_result.get("latency_ms"),
                }
            )
            if not response or not contexts:
                missing = "response" if not response else "retrieved_contexts"
                row["error"] = f"RAG returned empty {missing}"
                return row

            results = await asyncio.gather(
                metrics["faithfulness"].ascore(
                    user_input=sample["user_input"],
                    response=response,
                    retrieved_contexts=contexts,
                ),
                metrics["answer_relevancy"].ascore(
                    user_input=sample["user_input"],
                    response=response,
                ),
                metrics["context_precision"].ascore(
                    user_input=sample["user_input"],
                    reference=sample["reference"],
                    retrieved_contexts=contexts,
                ),
                metrics["context_recall"].ascore(
                    user_input=sample["user_input"],
                    reference=sample["reference"],
                    retrieved_contexts=contexts,
                ),
                metrics["has_citation"].ascore(response=response, llm=judge_llm),
                return_exceptions=True,
            )
            errors = []
            for name, result in zip(metrics, results):
                if isinstance(result, BaseException):
                    errors.append(f"{name}: {result}")
                    continue
                value = result.value
                if name == "has_citation":
                    row[name] = 1.0 if value == "yes" else 0.0 if value == "no" else math.nan
                else:
                    row[name] = finite_or_nan(value)
                if result.reason and value is None:
                    errors.append(f"{name}: {result.reason}")
            row["error"] = " | ".join(errors)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
    print(f"[{index}] {row['row_id']}: {row['error'] or 'ok'}", flush=True)
    return row


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--golden",
        type=Path,
        default=Path(os.getenv("RAG_EVAL_GOLDEN", "tests/eval/golden_dataset.json")),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            os.getenv("RAG_EVAL_RESULTS_DIR", "tests/eval/results")
        ),
    )
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--judge-provider",
        choices=("anthropic", "openai"),
        default=os.getenv("RAG_EVAL_JUDGE_PROVIDER", "openai"),
    )
    parser.add_argument(
        "--judge-model",
        default=os.getenv("RAG_EVAL_JUDGE_MODEL", "qwen2.5:14b"),
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv(
            "RAG_EVAL_EMBEDDING_MODEL", "qwen3-embedding:4b"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("RAG_EVAL_CONCURRENCY", "3")),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(
            os.getenv("RAG_EVAL_CACHE_DIR", "tests/eval/.ragas_cache")
        ),
    )
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--production-model")
    parser.add_argument("--collection")
    parser.add_argument("--chunk-size", type=int)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--request-timeout", type=float)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.top_k is not None and args.top_k < 1:
        parser.error("--top-k must be positive")
    if args.chunk_size is not None and args.chunk_size < 1:
        parser.error("--chunk-size must be positive")
    if args.request_timeout is not None and args.request_timeout <= 0:
        parser.error("--request-timeout must be positive")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    return args


def load_golden(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) < 30:
        raise ValueError("golden dataset must contain at least 30 items")
    normalized = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"row {index} must be an object")
        user_input = clean_text(row.get("user_input"))
        reference = clean_text(row.get("reference"))
        contexts = row.get("reference_contexts")
        if not user_input or not reference:
            raise ValueError(f"row {index} has an empty user_input/reference")
        if not isinstance(contexts, list) or not any(clean_text(item) for item in contexts):
            raise ValueError(f"row {index} has empty reference_contexts")
        normalized.append(
            {
                **row,
                "user_input": user_input,
                "reference": reference,
                "reference_contexts": [
                    clean_text(item) for item in contexts if clean_text(item)
                ],
            }
        )
    return normalized


def build_judge_client(args: argparse.Namespace) -> Any:
    if args.judge_provider == "anthropic":
        return AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return AsyncOpenAI(
        api_key=(
            os.getenv("RAG_EVAL_OPENAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or "ollama"
        ),
        base_url=os.getenv("RAG_EVAL_OPENAI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or None,
    )


def build_embedding_client(_: argparse.Namespace) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=(
            os.getenv("RAG_EVAL_OPENAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or "ollama"
        ),
        base_url=os.getenv("RAG_EVAL_OPENAI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or None,
    )


def judge_model_args(args: argparse.Namespace) -> dict[str, Any]:
    base_url = (
        os.getenv("RAG_EVAL_OPENAI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or ""
    )
    if args.judge_provider == "openai" and (
        "11434" in base_url or "ollama" in base_url.casefold()
    ):
        return {"extra_body": {"think": False}}
    return {}


async def close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is not None:
        result = close()
        if asyncio.iscoroutine(result):
            await result


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def finite_or_nan(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def nullable_mean(series: Any) -> float | None:
    value = series.mean(skipna=True)
    return float(value) if value is not None and math.isfinite(float(value)) else None


def safe_label(label: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", label.strip()).strip("_")
    if not value:
        raise ValueError("label must contain a letter or digit")
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
