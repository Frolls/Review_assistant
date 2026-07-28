#!/usr/bin/env python3
"""Run the fixed-dataset chunking and generation A/B experiments."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VARIANTS = (
    ("baseline", "rag_eval_chunk_256", 256, 10, None),
    ("chunk_512", "rag_eval_chunk_512", 512, 10, None),
    ("generation_qwen35", "rag_eval_chunk_512", 512, 10, "qwen3.5:9b"),
)


def main() -> None:
    args = parse_args()
    for label, collection, chunk_size, top_k, model_override in VARIANTS:
        production_model = model_override or args.production_model
        command = [
            sys.executable,
            "scripts/run_eval.py",
            "--label",
            label,
            "--judge-provider",
            "openai",
            "--judge-model",
            args.judge_model,
            "--embedding-model",
            args.embedding_model,
            "--production-model",
            production_model,
            "--collection",
            collection,
            "--chunk-size",
            str(chunk_size),
            "--top-k",
            str(top_k),
            "--concurrency",
            str(args.concurrency),
            "--request-timeout",
            str(args.request_timeout),
        ]
        print(f"Starting {label}: {' '.join(command)}", flush=True)
        subprocess.run(command, cwd=ROOT, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--judge-model",
        default=os.getenv("RAG_EVAL_JUDGE_MODEL", "qwen2.5:14b"),
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv(
            "RAG_EVAL_EMBEDDING_MODEL",
            "qwen3-embedding:4b",
        ),
    )
    parser.add_argument(
        "--production-model",
        default=os.getenv("RAG_EVAL_PRODUCTION_MODEL", "qwen3:latest"),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("RAG_EVAL_CONCURRENCY", "3")),
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=float(os.getenv("RAG_EVAL_REQUEST_TIMEOUT", "180")),
    )
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.request_timeout <= 0:
        parser.error("--request-timeout must be positive")
    return args


if __name__ == "__main__":
    main()
