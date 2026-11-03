#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
docker compose --project-directory "$PWD" --env-file .env -f Review_bot/deploy/compose.yaml --profile test run --rm --build tests ruff check --no-cache app tests
docker compose --project-directory "$PWD" --env-file .env -f Review_bot/deploy/compose.yaml --profile test run --rm --build tests
docker compose --project-directory "$PWD" --env-file .env -f Review_bot/deploy/compose.yaml --profile test run --rm --build bot-tests
sh Review_bot/scripts/test_pair.sh
