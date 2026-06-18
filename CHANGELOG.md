# Changelog

## [Unreleased]

### Added

- Added `OBSERVABILITY_INCLUDE_CONTENT` to control whether raw prompt/response content is emitted into observability spans.
- Added `PHOENIX_PROJECT_NAME` to configure the project label shown in Phoenix UI.
- Added a Linux-friendly `host.docker.internal:host-gateway` alias to Docker Compose for host-based backends such as local Ollama.

### Changed

- Renamed the default Phoenix project from `diploma-fastapi` to `ai-pr-review-assistant`.
- Updated `chat.request` spans to expose safe prompt/output previews, prompt hashes, lengths, cache status, and explicit span kind metadata.
- Configured OpenInference `ChatCompletion` spans to redact raw `LLM Input` and `LLM Output` when `OBSERVABILITY_INCLUDE_CONTENT=false`.
- Expanded observability documentation in `README.md`, `docs/observability/README.md`, and `docs/architecture.md` to describe safe-mode tracing, data storage, and expected Phoenix behavior.
- Refreshed the Phoenix trace screenshot to match the current observability configuration.

## [2026-06-17]

### Added

- Added review-assistant prompt helpers grounded in PEP, Ansible community documentation, internal style guides, and architecture documents.
- Added LLM response parsing and cost estimation helpers for LLM-adjacent application logic.
- Added an offline evaluation workflow with a versioned golden dataset, G-Eval-style judge script, threshold checks, and ignored local run artifacts.
- Added unit coverage for prompt construction, LLM parsing, schema validation, cost calculation, cache behavior, retry handling, and eval dataset helpers.

### Changed

- Expanded the README with the diploma project framing, testing workflow, offline evaluation commands, Ollama examples, and proxy guidance.
- Added pytest async/mock tooling and HTTPX to the development dependency group.

### Fixed

- Added explicit LLM quota error handling and retry-on-rate-limit behavior.
- Limited chat message length and masked PII in `ChatMessage` representations.

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
