#!/usr/bin/env python3
"""Send 20+ diverse RAG requests so Phoenix contains a diagnostic trace set."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.observability.tracing import setup_tracing  # noqa: E402
from app.services.rag import RAGService  # noqa: E402


FREE_QUESTIONS = [
    "Что проверить в Ansible task с shell pipeline?",
    "Когда для Ansible role нужен meta/main.yml?",
    "Почему type: ignore должен содержать error code?",
    "Чем Protocol отличается от ABC на архитектурной границе?",
    "Как защитить secret template при запуске --diff?",
    "Почему formatter-правки лучше отделять от изменения поведения?",
    "Когда TypedDict стоит заменить Pydantic-моделью?",
    "Что происходит с handler после нескольких notify?",
]


async def main() -> int:
    args = parse_args()
    payload = json.loads(args.golden.read_text(encoding="utf-8"))
    golden_rows = payload["items"] if isinstance(payload, dict) else payload
    questions = [row["user_input"] for row in golden_rows[: args.golden_count]]
    questions.extend(FREE_QUESTIONS)
    questions = questions[: args.total]
    if len(questions) < 20:
        raise ValueError("trace smoke requires at least 20 questions")

    settings = Settings()
    updates = {
        key: value
        for key, value in {
            "rag_collection": args.collection,
            "rag_similarity_top_k": args.top_k,
            "default_model": args.production_model,
        }.items()
        if value is not None
    }
    if updates:
        settings = settings.model_copy(update=updates)
    setup_tracing(observability_include_content=True)
    service = RAGService(settings)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def trace_one(index: int, question: str) -> None:
        async with semaphore:
            result = await service.evaluate_inputs(question)
        print(
            f"[{index}/{len(questions)}] confident={result['confident']} "
            f"contexts={len(result['retrieved_contexts'])} "
            f"latency_ms={result['latency_ms']}"
        )

    try:
        await service.build()
        await asyncio.gather(
            *(
                trace_one(index, question)
                for index, question in enumerate(questions, start=1)
            )
        )
    finally:
        await service.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--golden",
        type=Path,
        default=Path("tests/eval/golden_dataset.json"),
    )
    parser.add_argument("--golden-count", type=int, default=15)
    parser.add_argument("--total", type=int, default=23)
    parser.add_argument("--collection")
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--production-model")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    if args.total < 20:
        parser.error("--total must be at least 20")
    if args.top_k is not None and args.top_k < 1:
        parser.error("--top-k must be positive")
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
