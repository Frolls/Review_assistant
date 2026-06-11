# Changelog

## [Unreleased]

## [2026-06-11]

### Added

- Added a Phoenix observability stack to Docker Compose with persisted Phoenix data and collector wiring for the FastAPI app.
- Added OpenAI auto-instrumentation through Phoenix/OpenInference and explicit `gen_ai.*` span attributes for `/chat` requests.
- Added structured JSON logging with request-scoped context, `X-Request-ID` propagation, LLM token usage, latency, finish reason, prompt hash, and redacted prompt previews.
- Added PII masking helpers for email, Russian phone numbers, cards, INN, passport numbers, and optional Presidio-based Russian person-name anonymization.
- Added `docs/observability/phoenix-trace.png` and a short observability note describing what the trace screenshot shows.
- Added unit tests that guard against raw PII leaking into `prompt_preview`.

### Changed

- Limited supported Python versions to `>=3.11,<3.14` to match the current Presidio dependency range.
- Extended the Docker image build to include the `ru_core_news_md` spaCy model used by Presidio.

### Fixed

- Normalized Phoenix collector URLs to `/v1/traces` so spans export successfully when `PHOENIX_COLLECTOR_ENDPOINT` points at the Phoenix UI host.
- Updated Russian phone-number redaction to cover formats such as `+7 (999) 123-45-67` and `+7 999 123 45 67`.

## [2026-06-10]

### Added

- Added `GET /ready` readiness checks backed by `Redis.ping()` with degraded `503` responses when Redis is unavailable.
- Added dedicated health response schemas in `app/schemas/health.py`.
- Added centralized FastAPI router response metadata in `app/routers/responses.py`.
- Added a multi-stage `Dockerfile` based on `python:3.13-slim-bookworm` with `uv` and a non-root runtime user.
- Added `.dockerignore` to keep local caches, secrets, tests, and VCS metadata out of Docker build contexts.
- Added `compose.yaml` for running the FastAPI app together with Redis, health checks, and persistent Redis storage.
- Added `compose.override.yaml` for local Docker development with `uvicorn --reload` and a bind mount for `app/`.
- Added tests covering health and readiness behavior.
- Added a project changelog.

### Changed

- Updated `.env.example` with Docker-friendly defaults and clearer placeholder values.
- Expanded `README.md` with Docker Compose setup, self-check commands, and local development notes.
- Updated project dependencies: removed `jinja2`, constrained `openai` to `>=2.38.0,<3`, and added `ruff` to the `dev` dependency group.

### Fixed

- Updated the local settings example test to use explicit `.env`-style values so it no longer depends on whether the test process runs inside a container.

### Refactored

- Deduplicated repeated FastAPI `responses={...}` metadata across the `chat`, `models`, and `health` routers.
- Moved health response models out of the router module into shared schema definitions.

## [2026-06-06]

### Added

- Added a FastAPI-based LLM HTTP service with `/chat`, `/chat/stream`, `/health`, and `/models` endpoints.
- Added configuration loading, dependency providers, shared exception types, request tracing, and an LLM service layer.
- Added Pydantic schemas for chat payloads, streamed deltas, error envelopes, and model catalog responses.
- Added Redis-backed caching for synchronous chat completions.
- Added unit tests for the FastAPI LLM service.
- Added architecture documentation and LiteLLM configuration examples for local and production-like setups.

### Changed

- Reworked the project framing and setup docs around a PR review assistant, LiteLLM, and Ollama-compatible flows.
- Updated `.env.example`, `README.md`, and architecture docs to match the unified HTTP service direction.
- Updated devcontainer host access settings.

### Removed

- Removed the earlier tool-calling assistant implementation, including its review knowledge base, prompt templates, handlers, examples, and tests.

### Refactored

- Updated architecture documentation to describe the unified orchestrator and HTTP service design.

## [2026-06-03]

### Added

- Added an architecture passport (`ADR`) and project diagrams.
- Added LiteLLM sample configuration files and a mock OpenAI-compatible upstream server for documentation and local experiments.

## [2026-05-27]

### Added

- Added the initial review knowledge base, prompt loader, prompt templates, and tool definitions for review assistance workflows.
- Added a configurable LLM tool-calling client with environment-driven settings and dependency locking in `uv.lock`.
- Added runnable examples, automated tests, and README workflow documentation for the original tool-calling assistant flow.
- Added development container configuration for local work.

## [2026-04-28]

### Added

- Added pre-commit hooks configuration.
- Added README notes about the RAG data source used by the project.

## [2026-04-24]

### Added

- Added the initial project README scaffold.
