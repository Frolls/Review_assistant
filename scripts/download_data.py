#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path


PEP_NUMBERS = (
    8,
    20,
    257,
    263,
    287,
    333,
    343,
    380,
    405,
    440,
    451,
    484,
    487,
    492,
    508,
    517,
    518,
    544,
    561,
    563,
    585,
    589,
    604,
    612,
    621,
    634,
    635,
    636,
    646,
    647,
    649,
    654,
    660,
    668,
    695,
    701,
)
USER_AGENT = "corporate-rag-diploma-data-loader/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a reproducible corpus of official Python PEP documents."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "python-peps",
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def download_pep(number: int, output: Path, force: bool) -> tuple[int, str]:
    target = output / f"pep-{number:04d}-v1.0.html"
    if target.exists() and not force:
        return number, "unchanged"
    url = f"https://peps.python.org/pep-{number:04d}/"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        return number, f"failed: {exc}"
    if len(content) < 2_000 or b"<html" not in content[:1_000].lower():
        return number, "failed: response is not a valid HTML document"
    temporary = target.with_suffix(".html.part")
    temporary.write_bytes(content)
    temporary.replace(target)
    return number, "downloaded"


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args.output.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [
            pool.submit(download_pep, number, args.output, args.force)
            for number in PEP_NUMBERS
        ]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]

    counts: dict[str, int] = {}
    for number, outcome in sorted(results):
        status = outcome.split(":", 1)[0]
        counts[status] = counts.get(status, 0) + 1
        logging.info("PEP %04d: %s", number, outcome)
    available = len(list(args.output.glob("*.html")))
    logging.info("Corpus directory contains %d HTML documents; run summary: %s", available, counts)
    return 0 if available >= 30 else 1


if __name__ == "__main__":
    sys.exit(main())
