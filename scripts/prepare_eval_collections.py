#!/usr/bin/env python3
"""Build the two isolated Qdrant collections used by chunking A/B."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VARIANTS = (
    ("rag_eval_chunk_256", 256, "var/eval_chunk_256"),
    ("rag_eval_chunk_512", 512, "var/eval_chunk_512"),
)


def main() -> None:
    for collection, chunk_size, storage_dir in VARIANTS:
        environment = {
            **os.environ,
            "RAG_COLLECTION": collection,
            "RAG_CHUNK_SIZE": str(chunk_size),
            "RAG_CHUNK_OVERLAP": "32",
            "RAG_PIPELINE_STORAGE_DIR": storage_dir,
        }
        print(
            f"Preparing {collection}: chunk_size={chunk_size}, overlap=32",
            flush=True,
        )
        subprocess.run(
            [
                sys.executable,
                "scripts/ingest.py",
                "data/retrieval-corpus",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    main()
