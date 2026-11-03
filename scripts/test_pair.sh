#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
pair_bot_repo=${BOT_REPO:-../telegram-review-bot}
pair_bot_repo=$(cd "$pair_bot_repo" && pwd)
docker run --rm --network none -e OPENAI_API_KEY=test -e PHOENIX_TRACING_ENABLED=false \
  -v "$PWD/app:/app/app:ro" \
  -v "$pair_bot_repo/bot:/app/bot:ro" \
  -v "$PWD/integration/pair:/app/tests-pair:ro" \
  localhost/llm-service:v1 python -m pytest -q -p no:cacheprovider /app/tests-pair/pair_smoke.py
