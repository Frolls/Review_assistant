#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.ingestion import IngestionService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index PDF, DOCX, HTML and Markdown documents into Qdrant."
    )
    parser.add_argument("path", nargs="?", default="data", type=Path)
    parser.add_argument("--no-rename-failed", action="store_true")
    parser.add_argument("--show-progress", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = IngestionService(get_settings()).ingest_path(
        args.path,
        rename_failed=not args.no_rename_failed,
        show_progress=args.show_progress,
    )
    print(
        f"{result.changed} changed, {result.unchanged} unchanged, "
        f"{result.failed} failed; {result.chunks_written} chunks written"
    )
    print(json.dumps({"formats": result.formats, "failures": result.failures}, ensure_ascii=False))
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
