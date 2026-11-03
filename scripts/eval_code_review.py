"""Evaluate actual file review; human labels remain separate from model output.

Run from Review_bot: python scripts/eval_code_review.py --output review-run.json
Annotate each row's review fields, then use --score review-run.json.
Never executes model-generated code.
"""
import argparse
import ast
import asyncio
import json
import os
from pathlib import Path
import re
import time
import sys
from uuid import uuid4
from datetime import UTC, datetime

import httpx

CASES = Path(__file__).resolve().parents[1] / "tests/eval/code_review/cases.json"


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.review_eval import score  # noqa: E402


async def management_request(client, method, path, **kwargs):
    """Retry only idempotent chat management after the server's rate limit.

    Generation is never blindly resubmitted. Waiting is outside per-turn timing.
    """
    for attempt in range(3):
        response = await client.request(method, path, **kwargs)
        if response.status_code != 429 or attempt == 2:
            response.raise_for_status()
            return response
        delay = max(1, float(response.headers.get("Retry-After", "60")))
        print(json.dumps({"event": "management_rate_limit_wait", "seconds": delay}), flush=True)
        await asyncio.sleep(delay)


async def run(args):
    headers = {"X-Internal-Token": os.environ["INTERNAL_TOKEN"], "X-User-ID": "code-review-eval-" + uuid4().hex}
    rows = []
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=200) as client:
        ready = await client.get("/ready")
        ready.raise_for_status()
        Path(args.output).with_suffix(".ready.json").write_text(json.dumps(ready.json(), indent=2))
        for case in json.loads(Path(args.cases).read_text()):
            response = await management_request(client, "POST", "/chats", json={"owner_external_id": headers["X-User-ID"], "interface": "eval"})
            response.raise_for_status()
            chat_id = response.json()["chat_id"]
            cleared = await management_request(client, "DELETE", f"/chats/{chat_id}/messages")
            cleared.raise_for_status()
            started_utc = datetime.now(UTC).isoformat()
            start = time.monotonic()
            ext = "py" if case["language"] == "python" else "yaml"
            events = []
            timings = []
            error = None
            status = None
            request_id = None
            try:
                async with client.stream("POST", f"/chats/{chat_id}/messages", data={"content": case["task"]},
                                         files={"media": (f"review.{ext}", case["code"].encode(), "text/plain")}) as response:
                    status = response.status_code
                    request_id = response.headers.get("X-Request-ID")
                    if response.is_error:
                        error = (await response.aread()).decode()
                    else:
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                event = json.loads(line[6:])
                                events.append(event)
                                timings.append({"type": event.get("type"), "seconds": time.monotonic() - start})
            except (httpx.HTTPError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            latency = time.monotonic() - start
            text = "".join(e.get("delta", "") for e in events if e.get("type") == "token")
            history = await management_request(client, "GET", f"/chats/{chat_id}/messages")
            history.raise_for_status()
            saved = [message for message in history.json() if message.get("role") == "assistant"]
            metadata = saved[-1].get("media_refs", {}) if saved else {}
            syntax = []
            for code in re.findall(r"```python\s*\n(.*?)```", text, re.S):
                try:
                    ast.parse(code)
                    syntax.append(True)
                except SyntaxError:
                    syntax.append(False)
            rows.append({**case, "answer": text, "events": events, "latency_seconds": latency,
                         "started_at": started_utc, "request_id": request_id, "http_status": status, "transport_error": error,
                         "event_timings": timings, "saved_answer_metadata": metadata,
                         "python_syntax_checks": syntax,
                         "review": {"true_positives": None, "false_positives": None, "false_negatives": None, "fix_usable": None, "notes": ""}})
            Path(args.output).write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(json.dumps({"case": case["id"], "seconds": round(latency, 2), "status": metadata.get("review_answer", {}).get("status"), "error": error or next((e.get("message") for e in events if e.get("type") == "error"), None)}, ensure_ascii=False), flush=True)
        await management_request(client, "DELETE", f"/chats/{chat_id}/messages")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--output", default="review-run.json")
    parser.add_argument("--cases", default=str(CASES))
    parser.add_argument("--score")
    args = parser.parse_args()
    if args.score:
        print(json.dumps(score(json.loads(Path(args.score).read_text())), indent=2))
    else:
        asyncio.run(run(args))
