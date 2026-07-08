from __future__ import annotations

import argparse
import time

from app.services.embeddings import embed_texts


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test embedding cache latency.")
    parser.add_argument(
        "text",
        nargs="?",
        default="Как работает Redis cache-aside в /chat endpoint?",
    )
    args = parser.parse_args()

    started_at = time.perf_counter()
    vector = embed_texts([args.text])[0]
    duration_ms = (time.perf_counter() - started_at) * 1000
    print(f"dimensions={len(vector)} duration_ms={duration_ms:.2f}")


if __name__ == "__main__":
    main()
