"""Measure Ollama's native inference counters, independently of RAG quality.

Uses a public synthetic fixture only. Saves warmup and every repetition;
does not execute generated code or download models.
"""
import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen


def post(url, payload):
    request = Request(url, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=180) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3:latest")
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--thinking", choices=("off", "on", "both"), default="both")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    prompt = ("Проверь Python-код. Вызовы без cache должны возвращать независимые словари. "
              "Назови дефект и дай полную исправленную функцию. Ответ до 150 слов.\n"
              "def remember(value, cache={}):\n    cache[value] = True\n    return cache\n")
    payload = {"model": args.model, "stream": False, "keep_alive": "10m",
               "messages": [{"role": "user", "content": prompt}],
               "options": {"num_ctx": args.context, "num_predict": 1024,
                           "temperature": 0, "seed": 42}}
    report = {"scope": "native Ollama microbenchmark; no retrieval or application validation",
              "request": payload, "runs": []}
    for thinking in {"off": (False,), "on": (True,), "both": (False, True)}[args.thinking]:
        for repetition in range(args.repetitions + 1):
            started = time.monotonic()
            row = {"think": thinking, "repetition": repetition,
                   "warmup": repetition == 0, "started_at": datetime.now(UTC).isoformat()}
            try:
                response = post(args.url.rstrip("/") + "/api/chat", {**payload, "think": thinking})
                row["response"] = response
                for prefix in ("prompt_eval", "eval"):
                    duration = response.get(prefix + "_duration", 0) / 1e9
                    row[prefix + "_tokens_per_second"] = (
                        response.get(prefix + "_count", 0) / duration if duration else None)
            except (OSError, ValueError) as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            row["wall_seconds"] = time.monotonic() - started
            report["runs"].append(row)
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({key: value for key, value in row.items() if key != "response"}), flush=True)
    if any("error" in row for row in report["runs"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
