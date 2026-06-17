from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    args = parse_args()
    run_path = latest_run(Path(args.runs_dir))
    thresholds = load_thresholds(Path(args.thresholds))
    with run_path.open(encoding="utf-8") as fh:
        run = json.load(fh)

    aggregates = run.get("aggregates", {})
    failures = []
    for metric, threshold in thresholds.items():
        actual = aggregates.get(metric)
        if actual is None:
            failures.append(f"{metric}: missing aggregate, expected >= {threshold}")
            continue
        if float(actual) < threshold:
            failures.append(f"{metric}: {actual} < {threshold}")

    if failures:
        print(f"Threshold check failed for {run_path}:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Threshold check passed for {run_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check latest eval run thresholds.")
    parser.add_argument("--runs-dir", default="eval/runs")
    parser.add_argument("--thresholds", default="eval/thresholds.yaml")
    return parser.parse_args()


def latest_run(runs_dir: Path) -> Path:
    candidates = [path for path in runs_dir.glob("*.json") if path.is_file()]
    if not candidates:
        raise SystemExit(f"No eval run JSON files found in {runs_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_thresholds(path: Path) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                raise ValueError(f"Invalid threshold line: {raw_line.rstrip()}")
            key, value = line.split(":", 1)
            thresholds[key.strip()] = float(value.strip())
    return thresholds


if __name__ == "__main__":
    sys.exit(main())
