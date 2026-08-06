# Changelog

## [2026-08-06]

### Added

- Added a compact Chat Completions agent loop with allowlisted knowledge-base search, timezone-aware current time, and a local-only Telegram send stub.
- Added per-step agent traces with raw tool arguments, truncated results, token usage, duration, error reporting, and an optional `--trace` CLI output.
- Added score-guarded top-1 RAG fragment retrieval for simple synchronous tools and unit coverage for dispatch, unknown tools, provider failures, schemas, timezones, and the Telegram stub.
- Added five domain smoke-run logs covering successful delivery, missing knowledge, an unavailable tool, a long composite task, and a write-action provocation.

### Changed

- Configured the naive agent to use the shared `DEFAULT_MODEL` setting so the same OpenAI-compatible loop works with the project's selected Ollama or cloud model.

### Verified

- Verified the successful smoke task completes in three LLM steps and the out-of-domain task stops in two steps without an unbounded tool loop.
- Verified the composite task records an LLM timeout as a stable error result and the write-action run exposes the expected baseline confirmation risk without calling the Telegram API.
- Verified all `5` naive-agent unit tests and Ruff on the changed agent and test modules.

## [2026-07-28]

### Added

- Added a 35-question, manually reviewed RAGAS golden dataset alongside the raw `TestsetGenerator` output and timestamped per-row CSV/aggregate JSON artifacts.
- Added Docker-only evaluation tooling for RAGAS 0.4 `Faithfulness`, `AnswerRelevancy`, `ContextPrecision`, `ContextRecall`, and a structured `has_citation` discrete metric backed by a local Ollama judge.
- Added reproducible Python orchestration for eval collection preparation, chunking and generation A/B experiments, report generation, dependency verification, and a 23-query Phoenix trace smoke run.
- Added Phoenix/OpenInference tracing for the FastAPI RAG lifespan, including RAG root spans, LlamaIndex retriever and embedding spans, LLM prompt/response spans, similarity scores, and token usage.
- Added post-factum Phoenix `HallucinationEvaluator` annotations, a manual-review annotation for the single evaluator false positive, and screenshots of retrieval and annotation diagnostics.

### Changed

- Split evaluation and tracing packages into the `eval` and `tracing` optional dependency extras and added dedicated Docker build targets so production dependencies remain isolated.
- Replaced the legacy LlamaIndex OpenAI embedding integration with the project embedding adapter and pinned compatible LlamaIndex/OpenInference dependency lines.
- Added `RAGService.evaluate_inputs()` to return one generated answer and the full retrieved contexts from a single retrieval while leaving the `/rag/query` response contract unchanged.
- Replaced shell evaluation wrappers with project-native Python entry points and documented the Docker/Ollama workflow.
- Kept the final production baseline on `qwen3:latest`, `qwen3-embedding:4b`, chunk size/overlap `256/32`, top-K `10`, and re-ranking disabled.

### Fixed

- Fixed citation fallback detection so Python type syntax such as `list[str]` is not mistaken for a source marker and explicit numeric citations are scored deterministically.
- Fixed the hallucination workflow so evaluator labels, scores, explanations, and model metadata are written back to Phoenix span annotations instead of existing only in CSV files.
- Corrected the sole positive hallucination verdict through a separate human annotation after manual review showed that the answer correctly described Ansible `failed_when` AND/OR semantics.

### Verified

- Verified all three primary RAGAS runs at `35/35` successful rows: baseline, chunk size `512`, and `qwen3.5:9b` generation.
- Verified the selected baseline at faithfulness `0.777`, answer relevancy `0.810`, context precision `0.929`, context recall `0.981`, citation rate `1.000`, and mean latency `32.7 s`.
- Verified `23/23` final trace-smoke requests produce 23 RAG roots and 161 spans, including 46 retriever, 69 embedding, and 23 LLM spans with document scores and token usage.
- Verified Phoenix stores 23 hallucination annotations: the local judge marked `1/23`, while manual review confirmed `0/23` hallucinations and one evaluator false positive.
- Verified evaluation/tracing imports, `51` relevant tests, Ruff on every changed Python file, Compose validation, and successful production/eval Docker target builds.

## [2026-07-27]

### Added

- Added explicit PDF, DOCX, HTML, and Markdown ingestion in `app/services/ingestion.py`, including path-derived metadata, DOCX author extraction, stable document ids, and embedding metadata exclusions.
- Added a persistent LlamaIndex `IngestionPipeline` with `SimpleDocumentStore`, `DocstoreStrategy.UPSERTS`, `SentenceSplitter(256/32)`, OpenAI-compatible embeddings, and `QdrantVectorStore`.
- Added `scripts/ingest.py`, the reproducible PEP downloader, score calibration and multi-turn verification scripts, a 56-document domain corpus, and the data inventory.
- Added `POST /documents/upload` with HTTP 202, background incremental ingestion, live RAG refresh, and `.failed` handling for unreadable files.
- Added feedback source persistence through Alembic revision `20260727_0004`.

### Changed

- Rebuilt `app/services/rag.py` as a retrieval-first pipeline with top-10 retrieval, a pre-generation score guard, optional BGE re-ranking to top-5, numbered citations, structured sources, and `confident`.
- Integrated RAG into the existing Postgres-backed stateful chat flow, including optional history-aware condense for retrieval and token-by-token SSE with final `sources`, `confident`, and `message_id`.
- Changed the local deployment baseline to `qwen3-embedding:4b`, 2560-dimensional vectors, the `corporate_rag` collection, and a calibrated Compose threshold of `0.5`.
- Updated the root Compose stack to run ingestion and migrations automatically and to persist the corpus, ingestion docstore, Qdrant, PostgreSQL, and Redis data.
- Updated RAG, chat, inventory, README, and deployment documentation to match the implemented multi-format and multi-turn flow.

### Fixed

- Fixed out-of-corpus handling so answer generation is skipped and returns the exact grounded fallback while logging `rag.score_guard_refusal`.
- Fixed short follow-up retrieval when a local condense model returns an empty or subjectless rewrite by retaining the previous user question as an anchor.
- Fixed repeated unreadable uploads so every failed source receives a unique final `.failed` name.
- Fixed cold-start health reporting by allowing the initial 4B embedding pass to complete before the app is marked unhealthy.

### Verified

- Verified cold ingestion: `56 changed, 0 unchanged, 0 failed`, 1726 chunks; verified the repeated run: `0 changed, 56 unchanged`, 0 chunks.
- Verified live `/rag/query` citations at score `0.7598`, multi-turn Ansible retrieval at `0.7631`, and score-guard refusal at `0.2963 < 0.5` without answer generation.
- Verified PDF upload returns HTTP 202 and becomes retrievable in about 8 seconds; verified feedback and shown sources are persisted in PostgreSQL.
- Verified the root `docker compose up -d` starts app, bot, Qdrant, PostgreSQL, and Redis, and verified real Telegram polling and handled RAG updates.
- Verified the final backend suite: `93 passed, 6 skipped`; verified Ruff on all changed backend files.

## [2026-07-16]

### Added

- Added `app/services/chunking.py` with fixed-size, Russian-aware recursive, and semantic LlamaIndex splitters.
- Added `app/services/retrieval_eval.py` with dataset validation and macro-averaged Hit Rate@5, MRR@10, and Recall@10.
- Added `data/retrieval-corpus` with 10 focused PEP and Ansible documents containing 13,592 tokens.
- Added `tests/eval/retrieval_dataset.json` with 36 manually labelled Python/Ansible retrieval questions and one to three relevant sources per case.
- Added `app/services/reranker.py` with an optional `BAAI/bge-reranker-v2-m3` cross-encoder.
- Added `scripts/run_chunking_experiment.py`, `scripts/compare_embedding_latency.py`, and unit coverage for chunking, retrieval metrics, and re-ranking.
- Added `docs/chunking_experiment.md` with index statistics, strategy metrics, re-ranking results, parameter tuning, and the Qwen3 Embedding 4B/0.6B comparison.

### Changed

- Changed the RAG defaults to recursive chunking with `chunk_size=256`, `chunk_overlap=32`, and `top-K=10`.
- Updated `app/services/rag.py` to split on paragraph boundaries and Russian sentence boundaries.
- Kept `qwen3-embedding:4b` with 2560-dimensional vectors as the default after the 0.6B comparison.
- Updated `.env.example`, `README.md`, and RAG, embeddings, vector-store, and architecture documentation to match the implemented retrieval-evaluation pipeline.

### Verified

- Verified `docs_fixed`, `docs_recursive`, and `docs_semantic` with 34, 34, and 31 points respectively.
- Verified tuned recursive retrieval metrics: Hit Rate@5 `1.0000`, MRR@10 `0.9861`, and Recall@10 `1.0000`.
- Verified BGE re-ranking raises semantic MRR@10 from `0.9722` to `1.0000`; measured mean CPU latency is `10.50 s` per query.
- Verified `qwen3-embedding:0.6b` reduces semantic Hit Rate@5 to `0.9722` and MRR@10 to `0.9352`; measured mean query-embedding latency is `92.47 ms` versus `110.88 ms` for 4B.
- Verified Ruff on the changed Python files and pytest excluding two host-only ASGI timeouts: `82 passed, 6 skipped`.

## [2026-07-15]

### Added

- Added the Block 03 LlamaIndex RAG pipeline in `app/services/rag.py` with `SimpleDirectoryReader`, `SentenceSplitter`, Qdrant-backed `VectorStoreIndex`, a strict context-only QA prompt, score fallback, and source citation output.
- Added the matching bare-metal RAG implementation in `app/services/rag_baremetal.py` using explicit file loading, chunking, embeddings, Qdrant `query_points`, prompt assembly, and OpenAI-compatible chat completion.
- Added the dedicated `POST /rag/query` FastAPI endpoint with response schemas, router metadata, and a `503 rag_unavailable` path when the RAG index cannot be initialized.
- Added the 10-document review-domain corpus in `data/rag-block-03`, including one intentionally unrelated fallback-control document.
- Added `scripts/verify_rag_block03.py` for repeatable live evaluation of the required 3 good / 1 medium / 1 out-of-corpus question set.
- Added route coverage for `/rag/query`, including successful source output and unavailable-index behavior.

### Changed

- Added RAG configuration to settings and `.env.example`: corpus path, LlamaIndex and bare-metal collection names, chunk size, chunk overlap, top-k, and minimum top score.
- Made RAG initialization optional during FastAPI startup so the main service can still boot when Qdrant, Ollama, or LlamaIndex dependencies are unavailable.
- Updated the Docker image build to install dev dependencies by default, allowing tests to run inside the app image.
- Updated `README.md` and `docs/rag.md` with RAG configuration, endpoint usage, dependency versions, collection decisions, LlamaIndex vs bare-metal comparison, live HTTP verification, and factual 5-question evaluation results.

### Verified

- Verified `docker compose build app` builds the app image with dev dependencies, including `pytest`, `pytest-asyncio`, `pytest-mock`, and `ruff`.
- Verified full test suite inside the rebuilt app container: `78 passed, 6 skipped`.
- Verified `python -m app.services.rag` against Ollama and Qdrant returns `answer`, `top_score`, and 3 sources.
- Verified `python -m app.services.rag_baremetal` returns the same response contract and retrieves `ansible_best_practices.md` as top-1 for the Ansible control question.
- Verified live `POST /rag/query` via `curl` returns an answer for the secret-in-diff question with top-1 source `secure_code_review.md`.
- Verified the live 5-question RAG run: 3 good questions relevant, 1 medium question relevant through top-1/top-2 sources, and the tomato out-of-corpus question falls back at score `0.158 < 0.2`.

## [2026-07-10]

### Added

- Added Qdrant as the vector store service in `compose.yaml` with persisted storage, API key wiring, healthcheck, and `app` startup dependency.
- Added `app/services/vector_store.py` with a single `AsyncQdrantClient`, collection creation, payload indexes, batched upsert, and `query_points` search.
- Added structured review knowledge in `data/review_knowledge.json` with 14 PR-review domain documents and 140 chunks.
- Added `scripts/load_to_qdrant.py` for schema validation, deterministic point ids, embedding, idempotent Qdrant upsert, and COSINE/DOT comparison.
- Added unit coverage for the vector store wrapper and structured Qdrant loader.
- Added `docs/vector_store.md` with collection parameters, payload schema, load procedure, metric comparison, and filter examples.

### Changed

- Wired Qdrant settings into `Settings`, `.env.example`, FastAPI lifespan, and README documentation.
- Updated embedding configuration loading so standalone scripts read `.env` consistently with the FastAPI service.
- Updated `docs/embeddings.md` to describe the implemented Qdrant-backed RAG index instead of a future index.
- Constrained `qdrant-client` to `>=1.14.0,<1.16` to match the Qdrant 1.14 server line used by Docker Compose.

### Verified

- Verified `docker compose -p diploma up -d qdrant` starts Qdrant and exposes `documents` with `status=green`.
- Verified `python scripts/load_to_qdrant.py --batch-size 16` loads 140 chunks and a repeated run keeps `points_count=140`.
- Verified `python scripts/load_to_qdrant.py --batch-size 16 --compare-metrics` produces identical top-5 rankings for COSINE and DOT and removes temporary metric collections.
- Verified Qdrant collection parameters: vector size `2560`, distance `Cosine`, HNSW `m=16`, `ef_construct=100`, and payload indexes for `source`, `created_at`, `tenant_id`, `category`, `access_level`, and `archived`.
- Verified `pytest` reports `76 passed, 6 skipped`; verified `ruff check` on the Qdrant loader and tests.

## [2026-07-08]

### Added

- Added the reusable embedding service in `app/services/embeddings.py` with `embed_texts()`, `embed_query()`, and `embed_documents()` for the future RAG layer.
- Added batched OpenAI-compatible embedding calls, retry handling for transient provider errors, normalized vectors, and a persistent sqlite embedding cache.
- Added model-specific asymmetric retrieval handling: Qwen3 Embedding query instructions and E5 `query:` / `passage:` prefixes.
- Added `tests/eval/mini_benchmark.json` with review-domain query/relevant/irrelevant triples for the PR-review assistant corpus.
- Added `scripts/embedding_smoke.py` and `scripts/run_embedding_benchmark.py` for cache smoke checks and pairwise retrieval evaluation.
- Added `docs/embeddings.md` with the model-selection narrative, indexing cost estimate, cache verification, and benchmark results.

### Changed

- Chose `qwen3-embedding:4b` through local Ollama as the baseline RAG embedding model for the diploma corpus, with `text-embedding-3-small` documented as a cloud fallback.
- Updated `.env.example` with embedding configuration defaults and ignored `.cache/` so sqlite embedding caches stay local.
- Updated dependency metadata and `uv.lock` for the embedding stack.
- Expanded the README with the RAG embedding foundation, embedding environment variables, and the mini-benchmark command.

### Verified

- Verified local Ollama exposes `qwen3-embedding:4b` as an embedding model with 2560-dimensional vectors.
- Verified the embedding cache on a repeated smoke call: `1407.15 ms` cold path and `0.70 ms` cached path.
- Verified the mini-benchmark on `qwen3-embedding:4b`: `8/8` accuracy, mean margin `+0.2893`, minimum margin `+0.1644`, and cached rerun latency around `9.76 ms`.
- Verified `pytest tests/unit/test_embeddings.py tests/unit/test_eval_dataset.py` and `ruff check` for the embedding module, benchmark scripts, and tests.

## [2026-07-03]

### Added

- Added stateful chat moderation in `app/moderation` with keyword/regex rules, optional OpenAI Moderation API checks, masked incident logging, and persisted moderation incidents.
- Added admin API under `/chats/admin` protected by `X-Admin-Token`: 24h stats, recent users, broadcast queue creation, pending broadcast polling, and broadcast completion.
- Added message feedback API for `POST /chats/{chat_id}/messages/{message_id}/feedback` with Postgres `message_feedback` storage and `UNIQUE (owner_external_id, message_id)`.
- Added Postgres tables for moderation incidents, message feedback, broadcast queue, and assistant message latency through Alembic revision `20260703_0003`.
- Added root-level Docker Compose wiring for backend, Telegram bot, Postgres, Redis, and persisted `pg-data`.

### Changed

- Changed stateful chat SSE completion events to include the persisted assistant `message_id` so Telegram can attach feedback callbacks.
- Changed stateful chat output moderation to replace blocked assistant responses with a safe refusal text before persistence and streaming.
- Updated Postgres repository behavior to record moderation incidents and upsert feedback with `INSERT ... ON CONFLICT`.

### Verified

- Verified Docker Compose startup for `app`, `postgres`, and `redis`, including Alembic migration to `20260703_0003`.
- Verified moderation blocking with `403` and `detail.code == "moderation_blocked"` for forbidden input.
- Verified admin token protection, admin stats/users/broadcast endpoints, SSE `message_id`, and feedback upsert uniqueness against Postgres.
- Verified Telegram bot image build and live polling against the running backend.

## [2026-07-01]

### Added

- Added multipart media support to `POST /chats/{chat_id}/messages` for images, audio, PDF, and DOCX files while keeping the same endpoint URL.
- Added media extraction helpers: images are forwarded as multimodal `image_url` parts, audio is transcribed through Whisper-compatible `/audio/transcriptions`, and PDF/DOCX text is extracted with bounded limits.
- Added `media_refs` persistence for stateful chat history, including Postgres JSONB storage and an Alembic migration.
- Added `VISION_MODEL`, `LLM_NUM_CTX`, `BOT_URL`, and `INTERNAL_TOKEN` configuration.
- Added internal bot notification helper for the `/notify` backchannel.

### Changed

- Changed stateful chat message submission from JSON-only input to `multipart/form-data` with `content` and optional `media`.
- Changed stateful chat SSE output to JSON events with `token`, `done`, and `error` event types.
- Strengthened the Telegram-facing prompt and local domain guardrails for Python/Ansible review usage.
- Updated dependency metadata and `uv.lock` for media parsing and multipart handling.

## [2026-06-26]

### Added

- Added Telegram defaults for stateful chats, scoped to Python/Ansible PR review.

### Changed

- Made stateful chat creation idempotent and increased the streamed response budget.

## [2026-06-25]

### Added

- Added the stateful `/chats` API with server-side history, SSE responses, JSONL storage, and a Postgres repository backed by an Alembic migration.
- Added Russian documentation for the stateful chat module, `sliding`/`hybrid` context strategies, JSON/Postgres storage, and smoke verification.
- Added `curl` examples for chat creation, streaming message submission, history retrieval, and message clearing.

### Changed

- Updated the README with `CHAT_REPOSITORY`, `CHAT_STORAGE_DIR`, `CHAT_CONTEXT_STRATEGY`, `CHAT_CONTEXT_WINDOW`, and `DATABASE_URL`.
- Documented the smoke verification flow using `uvicorn`, `curl`, Redis, and an Ollama OpenAI-compatible backend.

### Verified

- Verified `GET /health`, `GET /ready`, `GET /models`, `POST /chat`, repeated cache-hit `POST /chat`, `POST /chat/stream`, and stateful `/chats` on a local Ollama and Redis setup.
- Confirmed that `POST /chat/stream` returns SSE chunks, usage, and final `data: [DONE]`, and that `/chats/{chat_id}/messages` persists user and assistant messages.
- Documented local setup limitations: an unavailable Phoenix collector emits retry warnings, and Ollama does not support `/v1/moderations` in the tested configuration, so moderation runs as a best-effort fallback.

## [2026-06-19]

### Added

- Added prompt-injection input validation, canary-backed output filtering, PII masking, and a best-effort moderation fallback for guarded `/chat` responses.
- Added Redis-backed HTTP rate limiting for `/chat` and `/chat/stream`, configurable with `RATE_LIMIT_PER_MIN`.
- Added garak REST target configuration, baseline/after security reports, and committed HTML report artifacts for the local Ollama-backed evaluation run.
- Added a synthetic load-test script that verifies the 31st request receives `429` when `RATE_LIMIT_PER_MIN=30`.
- Added guardrail tests for prompt blocking, canary leakage, output PII masking, and moderation fallback behavior.

### Changed

- Disabled Phoenix tracing through `PHOENIX_TRACING_ENABLED=false` for local security scans where Phoenix is not running.
- Extended structured log redaction so outgoing LLM response previews are masked before being emitted.
- Aligned the architecture load-management ADR with the implemented FastAPI Redis-backed rate limiter.

## [2026-06-18]

### Added

- Added `OBSERVABILITY_INCLUDE_CONTENT` to control whether raw prompt/response content is emitted into observability spans.
- Added `PHOENIX_PROJECT_NAME` to configure the project label shown in Phoenix UI.
- Added a Linux-friendly `host.docker.internal:host-gateway` alias to Docker Compose for host-based backends such as local Ollama.
- Added ADR-004 defining ownership of HTTP rate limiting, FastAPI concurrency control, LiteLLM quotas, and future batch workload isolation.
- Added initial TPM capacity values to the local and production-like LiteLLM deployment examples.
- Documented the target `full_pr_review` queue policy with a separate two-worker pool and dedicated LiteLLM quota and budget controls.

### Changed

- Renamed the default Phoenix project from `diploma-fastapi` to `ai-pr-review-assistant`.
- Updated `chat.request` spans to expose safe prompt/output previews, prompt hashes, lengths, cache status, and explicit span kind metadata.
- Configured OpenInference `ChatCompletion` spans to redact raw `LLM Input` and `LLM Output` when `OBSERVABILITY_INCLUDE_CONTENT=false`.
- Expanded observability documentation in `README.md`, `docs/observability/README.md`, and `docs/architecture.md` to describe safe-mode tracing, data storage, and expected Phoenix behavior.
- Refreshed the Phoenix trace screenshot to match the current observability configuration.
- Clarified that `LLM_MAX_CONCURRENCY` is a per-process FastAPI bulkhead rather than an HTTP or distributed rate limit.
- Clarified that LiteLLM deployment-level `rpm` and `tpm` values are router capacity metadata, not per-user quotas.
- Assigned external HTTP rate limiting to nginx/API Gateway in the target deployment architecture; no application-level rate limiter was added to FastAPI.
- Marked LiteLLM virtual keys, PostgreSQL-backed user quotas, nginx deployment configuration, and the `full_pr_review` worker pool as planned rather than currently implemented.

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
