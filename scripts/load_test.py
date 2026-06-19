#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Check /chat rate limiting.")
    parser.add_argument("--url", default="http://localhost:8000/chat")
    parser.add_argument("--requests", type=int, default=31)
    parser.add_argument("--user-id", default="load-test")
    args = parser.parse_args()

    statuses: list[int] = []
    payload = json.dumps(
        {
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0,
            "max_tokens": 8,
        }
    ).encode("utf-8")

    for _ in range(args.requests):
        request = urllib.request.Request(
            args.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-User-ID": args.user_id,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                statuses.append(response.status)
        except urllib.error.HTTPError as exc:
            statuses.append(exc.code)

    print("statuses:", statuses)
    if statuses[-1] != 429:
        print("expected the last request to receive HTTP 429", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
