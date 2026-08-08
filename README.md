# AI PR Review Assistant HTTP Service

HTTP-сервис на `FastAPI` для дипломного проекта «ИИ-ассистент для ревью кода». Цель ассистента — улучшать качество кода и сокращать время ревью pull request'ов. В качестве источников рекомендаций используются Python Enhancement Proposals (PEP), Ansible community documentation, внутренние руководства по стилю кода и архитектурные документы.

Сервис поднимает `app.main:app`, принимает запросы на `POST /chat`, `POST /chat/stream`, `POST /rag/query` и stateful API `/chats`, кеширует обычные ответы в `Redis`, работает с OpenAI-совместимым backend через `AsyncOpenAI` и экспортирует trace/span-данные в Phoenix.

На текущем этапе сервис отвечает за HTTP API, eval/testing слой, интеграцию с LLM backend и RAG-индекс в Qdrant:

- фронтенд, CLI или IDE-клиент отправляют вопросы по ревью PR в `/chat` или `/chat/stream`
- сервис валидирует входные данные, применяет защитный слой, выполняет логирование, читает и записывает кеш, нормализует ошибки
- OpenAI-совместимый backend выполняет генерацию ответа
- embedding-сервис готовит нормализованные векторы для базы знаний PR-review ассистента
- Qdrant хранит индекс `documents` с metadata-фильтрами по `source`, `created_at`, `tenant_id`, `category`, `access_level` и `archived`
- LlamaIndex RAG-индекс `corporate_rag` отвечает по 56 профильным HTML/Markdown-документам и принимает новые PDF/DOCX с цитатами на источники

Сервис не реализует собственную модель. Его зона ответственности: HTTP-контракт, вызов backend, кеширование, RAG orchestration, служебные endpoint'ы и инфраструктура проверки качества ответов.

## Что реализовано

- `POST /chat` — обычный completion-ответ с `cached: true/false`
- `POST /chat/stream` — SSE-поток с `data: ...` и финальным `data: [DONE]`
- `POST /rag/query` — синхронный RAG-ответ с `answer`, `confident`, `top_score` и списком цитируемых `sources`
- `POST /documents/upload` — загрузка PDF/DOCX/HTML/Markdown с фоновой UPSERT-индексацией (`202 Accepted`)
- `POST /chats` и `/chats/{chat_id}/messages` — stateful-чат с серверной историей, SSE-ответом и JSON/Postgres-хранилищем
- `POST /chats/{chat_id}/messages` принимает `multipart/form-data`: поле `content` и опциональный файл `media` для изображений, аудио, PDF и DOCX
- moderation для stateful-чата: keyword/regex слой из `app/moderation/moderation_keywords.yaml`, опциональный OpenAI Moderation API слой и безопасное логирование инцидентов без сырого текста
- `POST /chats/{chat_id}/messages/{message_id}/feedback` — сохранение 👍/👎 по ответу ассистента с защитой от дублей в Postgres
- `/chats/admin/*` — admin API для статистики, последних пользователей и broadcast-очереди, защищённое `X-Admin-Token`
- `GET /health` — liveness без зависимостей
- `GET /ready` — readiness с проверкой `Redis`
- `GET /models` — статический каталог OpenAI-моделей с ценами
- Redis-backed rate limiting для `/chat` и `/chat/stream` по `X-User-ID` или IP
- защитный слой для `/chat`: prompt-injection validator, canary-token в system prompt, output filter, PII masking и best-effort moderation
- `X-Request-ID`, request logging, CORS и единый формат ошибок
- structured JSON logs с `request_id`, latency, token usage, finish reason, `prompt_hash` и безопасным `prompt_preview`
- OpenInference/Phoenix tracing для `/chat` и LLM-вызовов с `gen_ai.*` атрибутами
- PII-redaction для email, российских телефонов, карт, ИНН и паспортов перед записью prompt/output preview в логи
- embedding-сервис для RAG: `embed_texts()`, `embed_query()`, `embed_documents()`, батчинг, retry на сетевые сбои, sqlite-кеш между рестартами и нормализация векторов
- multi-format LlamaIndex ingestion: explicit PDF/DOCX/HTML/Markdown readers → metadata enrichment → `SentenceSplitter(256/32)` → `OpenAIEmbedding` → `QdrantVectorStore`
- persistent `SimpleDocumentStore` with `DocstoreStrategy.UPSERTS`, so unchanged documents are not embedded again
- retrieval-first query pipeline: optional history-aware condense → top-10 retrieval → score guard → optional BGE re-ranking to top-5 → numbered context and citations
- recursive chunking с границами абзацев и русских предложений; выбранный конфиг `256/32`, `top-K=10`
- bare-metal RAG pipeline для сравнения: чтение файлов, чанкинг, embeddings, Qdrant `query_points`, сборка prompt и OpenAI-compatible chat completion без LlamaIndex
- retrieval evaluation на 36 Python/Ansible-вопросах и 10 документах: Hit Rate@5, MRR@10, Recall@10, A/B chunking и embedding-моделей
- optional re-ranker `BAAI/bge-reranker-v2-m3` для offline retrieval evaluation
- mini-benchmark для проверки retrieval-поведения на проектных вопросах из области PR-review ассистента
- быстрый unit testing layer вокруг LLM-adjacent логики и отдельный offline evaluation layer в `eval/`
- standalone-сравнение naive и управляемого ReAct tool loop с жёсткими лимитами, self-reflection и полным token usage
- standalone LangGraph 1.x orchestration в двух вариантах: явный типизированный
  `StateGraph` и prebuilt `langchain.agents.create_agent`, с единым набором tools,
  force-finish и сравнением с naive loop
- security evaluation layer на базе NVIDIA garak с baseline/after отчётами

## Архитектура

```text
Client
  -> FastAPI app.main:app
  -> Phoenix collector/UI
  -> OpenAI-compatible backend
     -> OpenAI API
     -> LiteLLM Proxy
     -> Ollama embeddings
```

Для режима с LiteLLM приложение по-прежнему использует `AsyncOpenAI`, но:

- `OPENAI_API_KEY` содержит master key LiteLLM Proxy
- `OPENAI_BASE_URL` указывает на proxy, например `http://localhost:4000`
- `DEFAULT_MODEL` содержит public model id, который proxy принимает, например `gpt-4.1-mini`

Для локального режима с Ollama приложение использует тот же OpenAI-compatible клиент:

- `OPENAI_API_KEY=ollama`
- `OPENAI_BASE_URL=http://host.docker.internal:11434/v1` или `http://localhost:11434/v1`
- `DEFAULT_MODEL=qwen3` или другая локально скачанная модель
- `VISION_MODEL=qwen2.5vl:7b` или другая vision-модель, если нужно анализировать изображения
- `EMBEDDING_MODEL=qwen3-embedding:4b` для локального RAG-индекса

## Основные файлы

```text
app/
  main.py
  core/
    config.py
    exceptions.py
  observability/
    logging.py
    pii.py
    tracing.py
  deps/
    providers.py
  routers/
    chat.py
    documents.py
    health.py
    models.py
    rag.py
  schemas/
    chat.py
    models.py
    rag.py
  services/
    agent_graph.py
    agent_naive.py
    agent_react.py
    chunking.py
    embeddings.py
    ingestion.py
    llm.py
    rag.py
    rag_baremetal.py
    reranker.py
    retrieval_eval.py
    vector_store.py
    security/
      input_validator.py
      output_filter.py
  chat/
    domain.py
    feedback.py
    repository.py
    service.py
    routes.py
    deps.py
    repositories/
  moderation/
    service.py
    moderation_keywords.yaml
  admin/
    auth.py
    routes.py
docs/
  agent-graph-custom.mmd
  agent-graph-prebuilt.mmd
  agent-graph-report.md
  agent-graph-results.json
  agent-react-report.md
  agent-react-results.json
  chat.md
  architecture.md
  chunking_experiment.md
  data_inventory.md
  embeddings.md
  rag.md
  rag_score_distribution.json
  vector_store.md
  observability/
    README.md
    phoenix-trace.png
  security/
    garak_baseline_2026-06-19.md
    garak_after_2026-06-19.md
    reports/
  litellm/
    config.yaml
    config.production_like.yaml
scripts/
  bench_agents.py
  compare_agents.py
  calibrate_rag_threshold.py
  compare_embedding_latency.py
  download_data.py
  embedding_smoke.py
  ingest.py
  load_test.py
  run_chunking_experiment.py
  run_embedding_benchmark.py
  verify_multiturn.py
  visualize_graph.py
data/
  python-peps/
  retrieval-corpus/
  review_knowledge.json
  rag-block-03/
tests/
  eval/
    retrieval_dataset.json
  unit/
  test_observability_pii.py
  test_security_guardrails.py
  test_llm_service.py
eval/
  security/
    rest_config.json
  golden_dataset.json
  run_evaluation.py
  check_thresholds.py
  thresholds.yaml
```

## Переменные окружения

Шаблон лежит в `.env.example`.

Обязательные ключи приложения:

- `OPENAI_API_KEY`
- `DEFAULT_MODEL`
- `REQUEST_TIMEOUT`
- `REDIS_URL`
- `CACHE_TTL_SECONDS`
- `RATE_LIMIT_PER_MIN`
- `CORS_ORIGINS`

Дополнительно:

- `OPENAI_BASE_URL` — адрес LiteLLM или другого OpenAI-compatible backend
- `EMBEDDING_PROVIDER` — provider embedding-модели: `openai` для OpenAI-compatible endpoint'ов или `sentence-transformers` для локальной Python-модели. Для `sentence-transformers` установите optional extra `local-embeddings`, потому что он тянет PyTorch.
- `EMBEDDING_MODEL` — модель для RAG-векторов; локальный выбор проекта — `qwen3-embedding:4b`
- `EMBEDDING_BATCH_SIZE` — размер батча embedding-запросов; для OpenAI-compatible endpoint'ов по умолчанию используется `128`
- `EMBEDDING_DIMENSIONS` — опциональное сокращение размерности для моделей, которые поддерживают параметр `dimensions`
- `EMBEDDING_DIM` — размерность vectors в Qdrant; для `qwen3-embedding:4b` используется `2560`
- `EMBEDDING_CACHE_PATH` — sqlite-файл кеша embeddings, по умолчанию `.cache/embeddings.sqlite`
- `EMBEDDING_REQUEST_TIMEOUT` — timeout embedding-запроса к provider
- `QDRANT_URL` — URL Qdrant; локально `http://localhost:6333`, в compose `http://qdrant:6333`
- `QDRANT_API_KEY` — API key Qdrant; в production должен передаваться из secret manager
- `QDRANT_COLLECTION` — имя коллекции vector store, по умолчанию `documents`
- `RAG_INPUT_DIR` — каталог основного RAG-корпуса, по умолчанию `data`
- `RAG_PIPELINE_STORAGE_DIR` — постоянный LlamaIndex docstore для определения changed/unchanged, по умолчанию `var/ingestion`
- `RAG_COLLECTION` — основная LlamaIndex-коллекция Qdrant, по умолчанию `corporate_rag`
- `RAG_BAREMETAL_COLLECTION` — отдельная bare-metal коллекция Qdrant, по умолчанию `rag_block_03_diploma_baremetal`
- `RAG_CHUNK_SIZE` и `RAG_CHUNK_OVERLAP` — параметры чанкинга, по умолчанию `256` и `32`
- `RAG_SIMILARITY_TOP_K` — число retrieval-кандидатов, по умолчанию `10`, минимум `3`
- `RAG_SCORE_THRESHOLD` — порог честного fallback-а до вызова LLM: кодовый default `0.3`, для `qwen3-embedding:4b` откалибровано `0.5`
- `RAG_CONDENSE_ENABLED` — переписывает короткий follow-up в самостоятельный поисковый запрос с учётом истории
- `RAG_RERANKER_ENABLED` — включает optional BGE re-ranking после score guard
- `RAG_RERANKER_MODEL` — optional cross-encoder для retrieval evaluation, по умолчанию `BAAI/bge-reranker-v2-m3`
- `RAG_RERANKER_TOP_N` — число кандидатов после re-rank, по умолчанию `5`
- `VISION_MODEL` — модель для stateful-чата с изображениями; если не задана, используется `DEFAULT_MODEL`
- `LLM_NUM_CTX` — опциональный размер контекста для OpenAI-compatible backend'ов, которые принимают `extra_body.options.num_ctx`
- `LLM_MAX_CONCURRENCY` — ограничение параллелизма
- `SECURITY_GUARDRAILS_ENABLED` — включает input validator, canary system prompt, output filter и moderation fallback; выключать только для контролируемого garak baseline
- `LOG_LEVEL` — уровень логирования приложения, по умолчанию `INFO`
- `OBSERVABILITY_INCLUDE_CONTENT` — включает сырой `input/output` в span-атрибутах; по умолчанию `false`
- `PHOENIX_TRACING_ENABLED` — включает экспорт trace в Phoenix; для локальных security scans без Phoenix можно поставить `false`
- `PHOENIX_COLLECTOR_ENDPOINT` — OTLP HTTP endpoint Phoenix, по умолчанию `http://localhost:6006`
- `PHOENIX_PROJECT_NAME` — имя проекта в Phoenix UI, по умолчанию `ai-pr-review-assistant`
- `CHAT_REPOSITORY` — хранилище stateful-чата: `json` или `postgres`, по умолчанию `json`
- `CHAT_STORAGE_DIR` — базовый каталог JSONL-хранилища для `/chats`, по умолчанию `./var/chats`
- `CHAT_CONTEXT_STRATEGY` — стратегия контекста `sliding` или `hybrid`
- `CHAT_CONTEXT_WINDOW` — количество последних сообщений, сохраняемых в prompt перед token-budget trimming
- `DATABASE_URL` — async SQLAlchemy URL, обязателен для `CHAT_REPOSITORY=postgres`
- `BOT_URL` — внутренний URL Telegram-бота для backchannel-уведомлений, по умолчанию `http://localhost:8081`
- `INTERNAL_TOKEN` — общий внутренний токен backend/bot для `/notify`
- `ADMIN_TOKEN` — общий admin-токен для `/chats/admin/*` и Telegram admin-команд
- `MODERATION_OPENAI_ENABLED` — включает дополнительный OpenAI Moderation API слой для stateful `/chats`, по умолчанию `false`
- `MODERATION_KEYWORDS_PATH` — путь к YAML-файлу keyword/regex правил, по умолчанию `app/moderation/moderation_keywords.yaml`

`PHOENIX_COLLECTOR_ENDPOINT`, если указывает только на хост Phoenix UI, автоматически нормализуется до `/v1/traces`. Например, `http://localhost:6006` будет использован как `http://localhost:6006/v1/traces`.

В production-like режиме рекомендуется оставлять `OBSERVABILITY_INCLUDE_CONTENT=false`: тогда в span'ы пишутся безопасные метаданные вроде `llm.prompt_hash`, `llm.prompt_preview`, `llm.output_preview`, длины текста и usage-метрик, а `input.value`/`output.value` заменяются на `[redacted]`.

## Observability

Подсистема observability включает:

- structured JSON logs на базе `structlog`;
- trace и span-данные Phoenix/OpenInference для HTTP- и LLM-вызовов;
- группировку trace в Phoenix по имени проекта из `PHOENIX_PROJECT_NAME`, по умолчанию `ai-pr-review-assistant`.

Structured logs сохраняют контекст запроса и выполнения, включая `request_id`,
`user_id`, `path`, `method`, HTTP status и latency. Исходный prompt целиком в
логи не записывается; preview исходящих LLM-ответов также проходит через PII
redaction. Вместо полного prompt используются:

- `prompt_hash` — короткий стабильный SHA-256 digest для корреляции похожих запросов;
- `prompt_preview` — редактированное preview с маскированием email, телефона, карты, ИНН, паспорта и, для длинных текстов, опциональной anonymization имён через Presidio + spaCy.

Для trace-данных рекомендуется production-like конфигурация с
`OBSERVABILITY_INCLUDE_CONTENT=false`. В этом режиме:

- span `chat.request` сохраняет только безопасные атрибуты, включая `llm.prompt_hash`, `llm.prompt_preview`, `llm.output_preview`, длины текста, cache status и usage-метрики;
- `input.value` и `output.value` в `chat.request` заменяются на `[redacted]`;
- auto-instrumented span `ChatCompletion` скрывает сырой `LLM Input` и `LLM Output`; в Phoenix эти поля отображаются как `__REDACTED__`.

При запуске observability-стека из `Review_bot/compose.yaml` или корневого
`compose.yaml` trace Phoenix хранятся в volume `phoenix-data` в базе
`/data/phoenix.db`.
Structured logs сохраняются в stdout контейнера `app`. Кеш ответов хранится
отдельно в `Redis`.

Подробное описание конфигурации и ожидаемого поведения приведено в
`docs/observability/README.md`.

## Testing и evaluation

Eval-зависимости не устанавливаются на хост и не входят в production image.
Проверка импортов и полный A/B-прогон выполняются в одноразовом Docker image:

```bash
docker compose up -d qdrant phoenix
docker compose --profile eval run --rm eval python scripts/verify_eval.py
docker compose --profile eval run --rm eval python scripts/prepare_eval_collections.py
docker compose --profile eval run --rm eval python scripts/run_ab_evals.py
docker compose --profile eval run --rm eval python scripts/build_eval_report.py
```

Локальный judge использует OpenAI-compatible endpoint Ollama:
`qwen2.5:14b`; judge embeddings — `qwen3-embedding:4b`. Облачные ключи не
требуются. Golden dataset и timestamped CSV/JSON лежат в `tests/eval/`,
итоговый отчёт — в `docs/rag_evaluation.md`.

Сырые пары TestsetGenerator также создаются внутри eval image:

```bash
docker compose --profile eval run --rm eval \
  python scripts/generate_golden.py --size 32 --model qwen2.5:14b
```

После генерации `tests/eval/golden_dataset_raw.csv` обязательно вычитывается
вручную; рабочий `golden_dataset.json` не заменяется сырым CSV автоматически.

Embedding mini-benchmark проверяет, что выбранная embedding-модель ставит
релевантный фрагмент выше нерелевантного для review-вопросов по Python, Ansible,
security и architecture guidelines. Проверенный локальный прогон на
`qwen3-embedding:4b` дал `8/8` и средний margin `+0.2893`:

```bash
OPENAI_API_KEY=ollama \
OPENAI_BASE_URL=http://host.docker.internal:11434/v1 \
EMBEDDING_PROVIDER=openai \
EMBEDDING_MODEL=qwen3-embedding:4b \
uv run python scripts/run_embedding_benchmark.py
```

Повторный запуск использует sqlite-кеш из `EMBEDDING_CACHE_PATH`: в локальной
проверке latency снизилась примерно с `3142 ms` до `10 ms`.

Retrieval-eval использует 10 документов из `data/retrieval-corpus` и
`tests/eval/retrieval_dataset.json` с 36 вручную размеченными
Python/Ansible-вопросами. Полный прогон с BGE
re-ranker требует optional extra:

```bash
uv sync --extra local-embeddings
uv run python scripts/run_chunking_experiment.py --qdrant-path .cache/qdrant-eval
uv run python scripts/compare_embedding_latency.py
```

Без re-ranker тяжёлая optional-зависимость не нужна:

```bash
uv run python scripts/run_chunking_experiment.py \
  --qdrant-path .cache/qdrant-eval \
  --skip-reranker
```

Фактические метрики, параметрическая сетка и сравнение
`qwen3-embedding:4b`/`0.6b` зафиксированы в
`docs/chunking_experiment.md`.

Основная Qdrant-коллекция `/rag/query` называется `corporate_rag` и загружается
из multi-format корпуса `data/`. Архитектура, калибровка score guard и HTTP/SSE
контракты описаны в `docs/rag.md`.

## Naive, ReAct и LangGraph агенты

Для сравнения orchestration-подходов сохранены три standalone-модуля:

- `app/services/agent_naive.py` — baseline с обычным `for`-циклом по
  `max_steps`; алгоритм сохраняется как поведенческий regression baseline;
- `app/services/agent_react.py` — native ReAct поверх Chat Completions tool
  calling: один tool за итерацию, строгие JSON Schema, critic после каждого
  observation, не более двух ревизий и явные остановки по timeout/числу
  итераций;
- `app/services/agent_graph.py` — LangGraph 1.x custom/prebuilt реализация,
  описанная ниже.

ReAct использует `gpt-5.4-mini` для main/critic и `gpt-5.4` для следующего шага
после `REVISE`. Диапазоны параметров ограничены в коде:
`max_iterations=8..20`, `timeout_per_iteration_sec=5..15`,
`max_revisions=0..2`. Все три реализации не подключены к HTTP endpoint'ам и
служат проверяемыми прототипами application-layer orchestrator.

Локальный запуск naive/ReAct вариантов через установленную модель Ollama
`qwen3:latest`:

```bash
DEFAULT_MODEL=qwen3:latest uv run python -m app.services.agent_naive \
  "Какое сейчас время в Asia/Yekaterinburg?" --trace

uv run python -m app.services.agent_react \
  "Какое сейчас время в Asia/Yekaterinburg?" \
  --timeout 15 \
  --model-main qwen3:latest \
  --model-critic qwen3:latest \
  --model-revision qwen3:latest \
  --trace
```

Полный повторяемый A/B-прогон пяти задач, включая две composability-задачи и
провокацию без tool call:

```bash
docker compose up -d qdrant
docker compose --profile eval run --rm --no-deps eval \
  python scripts/compare_agents.py \
  --model qwen3:latest \
  --react-timeout 15 \
  --output docs/agent-react-results.json
```

Итоговая таблица и наблюдения находятся в
[docs/agent-react-report.md](docs/agent-react-report.md), полные traces — в
[docs/agent-react-results.json](docs/agent-react-results.json). В проверенном
прогоне ReAct решил `5/5` задач, naive — `2/5`; суммарный usage ReAct включает
вызовы main и critic.

Следующий orchestration-инкремент находится в
`app/services/agent_graph.py`. Он не заменяет HTTP-маршруты или предыдущий
ReAct+reflection прототип и содержит два независимо вызываемых runnable с одним
набором `@tool`:

- `custom_graph` — ручной `StateGraph` с `AgentState`, узлами `call_model`,
  `execute_tool`, `force_finish` и отдельным router;
- `prebuilt_graph` — стандартный агент через
  `langchain.agents.create_agent`.

State custom-графа хранит только сериализуемые `messages`,
`iteration_count` и накопительные `tool_results`. При шестом model-шаге router
всегда направляет выполнение в `force_finish`. В benchmark custom-граф уже
получает `thread_id`; пока graph compiled без checkpointer, это no-op, но
интерфейс готов для `AsyncSqliteSaver` или `AsyncPostgresSaver`.

Установка и проверка LangGraph 1.x:

```bash
uv sync
uv run python -c \
  "from importlib.metadata import version; print(version('langgraph'))"
```

Пакет `langgraph` 1.2 не экспортирует `langgraph.__version__`, поэтому версия
читается через стандартный `importlib.metadata`.

Генерация Mermaid-схем и полный benchmark через локальный Ollama/Qdrant:

```bash
docker compose up -d qdrant

OPENAI_BASE_URL=http://localhost:11434/v1 \
QDRANT_URL=http://localhost:6333 \
uv run python scripts/visualize_graph.py

OPENAI_BASE_URL=http://localhost:11434/v1 \
QDRANT_URL=http://localhost:6333 \
uv run python scripts/bench_agents.py \
  --model qwen3:latest \
  --base-url http://localhost:11434/v1 \
  --repetitions 3
```

Benchmark выполняет `5 задач × 3 реализации × 3 повтора`, циклически меняет
порядок реализаций и сохраняет сырые результаты в
[docs/agent-graph-results.json](docs/agent-graph-results.json). Итоговый отчёт,
state contract, stop conditions и вывод custom vs prebuilt находятся в
[docs/agent-graph-report.md](docs/agent-graph-report.md). Проверенный прогон на
`qwen3:latest` дал `15/15` корректных запусков для custom и prebuilt; naive
завершился без технических ошибок, но не прошёл composability и проверку
безопасного отказа на провокации.

## Multi-format RAG

Основной корпус содержит 56 профильных документов по Python/PEP, Ansible и
code review. Ingestion поддерживает PDF, DOCX, HTML и Markdown, обогащает
metadata и использует постоянный docstore с `DocstoreStrategy.UPSERTS`.

Индексирование:

```bash
python scripts/ingest.py data/
```

HTTP endpoint:

```bash
curl -sS -X POST http://localhost:8000/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"Что делать, если секрет уже попал в diff?"}'
```

Ответ содержит `answer`, `confident`, `top_score` и до
`RAG_RERANKER_TOP_N` элементов в `sources`; ограничение применяется и при
выключенном re-ranker. Индекс подключается
в `lifespan`; если RAG недоступен на старте, `/rag/query`
возвращает `503` с `detail.code == "rag_unavailable"`, не останавливая
весь FastAPI-сервис.

Профильная multi-turn проверка:

```bash
python scripts/verify_multiturn.py
```

Фактические параметры и результаты калибровки описаны в `docs/rag.md`.

Для локального smoke/full-прогона через Ollama можно использовать проверенную пару `qwen2.5:14b` + `qwen2.5:14b`:

```bash
OPENAI_API_KEY=ollama \
OPENAI_BASE_URL=http://host.docker.internal:11434/v1 \
DEFAULT_MODEL=qwen2.5:14b \
REQUEST_TIMEOUT=180 \
uv run python eval/run_evaluation.py \
  --golden eval/golden_dataset.json \
  --judge qwen2.5:14b \
  --out eval/runs/$(date +%F)-ollama.json \
  --max-tokens 550 \
  --judge-max-tokens 700
uv run python eval/check_thresholds.py
```

Если для доступа к LLM API нужен proxy, задайте его локально через переменную окружения, не записывая URL с логином/паролем в репозиторий:

```bash
EVAL_PROXY='http://user:password@proxy.example.com:8888' \
  uv run python eval/run_evaluation.py --golden eval/golden_dataset.json --judge gpt-5.2
```

`eval/golden_dataset.json` содержит версионированный golden dataset по ревью Python/Ansible/архитектурных изменений, а `eval/check_thresholds.py` сверяет последний run с порогами из `eval/thresholds.yaml`.

### Security evaluation

Для prompt-injection/security smoke используется NVIDIA garak. REST-конфиг под
форму `/chat` лежит в `eval/security/rest_config.json`: garak подставляет `$INPUT`
в `messages[0].content` и читает ответ из поля `content`.

Минимальный проверенный набор проб:

```bash
garak --target_type rest -G eval/security/rest_config.json \
  --probes promptinject.HijackHateHumans,encoding.InjectBase64,dan.Ablation_Dan_11_0 \
  --generations 1 \
  --report_prefix baseline
```

Для baseline сервис запускается с `SECURITY_GUARDRAILS_ENABLED=false`, для after —
с `SECURITY_GUARDRAILS_ENABLED=true`. В локальном прогоне использовался Ollama
backend `llama3.2:3b`; отчёты сохранены в `docs/security/`.

Проверка rate limit требует доступного Redis и поднятого FastAPI. Для лимита
`RATE_LIMIT_PER_MIN=30` скрипт ожидает, что 31-й запрос получит `429`:

```bash
scripts/load_test.py --url http://127.0.0.1:8000/chat --requests 31 --user-id load-test
```

## Быстрый старт через LiteLLM

1. Установить зависимости приложения:

```bash
uv sync
```

2. Поднять `Redis`.

3. Подготовить `.env`:

```env
OPENAI_API_KEY=sk-review-bot-local
OPENAI_BASE_URL=http://localhost:4000
DEFAULT_MODEL=gpt-4.1-mini
REQUEST_TIMEOUT=30
REDIS_URL=redis://localhost:6379/0
CACHE_TTL_SECONDS=300
LLM_MAX_CONCURRENCY=5
RATE_LIMIT_PER_MIN=30
SECURITY_GUARDRAILS_ENABLED=true
CORS_ORIGINS=[]
LOG_LEVEL=INFO
PHOENIX_TRACING_ENABLED=true

OPENAI_UPSTREAM_API_KEY=
ANTHROPIC_API_KEY=
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
OLLAMA_API_KEY=ollama
```

4. При необходимости поднять Phoenix UI для локального просмотра trace из
   каталога `Review_bot/`:

```bash
docker compose up -d phoenix
```

Phoenix UI будет доступен на `http://127.0.0.1:6006`.

5. Поднять LiteLLM Proxy:

```bash
uv tool install 'litellm[proxy]'
litellm --config docs/litellm/config.production_like.yaml --port 4000
```

6. Поднять FastAPI:

```bash
uv run uvicorn app.main:app --reload
```

7. Открыть Swagger:

```text
http://localhost:8000/docs
```

## Запуск через Docker Compose

Каноничный compose-файл находится в корне workspace рядом с каталогами
`Review_bot/` и `telegram-review-bot/`. Он поднимает `app`, `bot`, `postgres`,
`redis` и `qdrant`. Корпус, ingestion docstore и данные сервисов сохраняются
в именованных volumes.

1. Из корня workspace подготовить `.env`:

```bash
cp .env.example .env
```

2. Заполнить `BOT_TOKEN` и параметры OpenAI-compatible backend. Локальная
   конфигурация рассчитана на уже запущенный Ollama с `qwen3:8b` и
   `qwen3-embedding:4b`.

3. Поднять стек:

```bash
docker compose up -d
```

На первом запуске контейнер `app` индексирует встроенный корпус до запуска
Uvicorn. Для локальной 4B embedding-модели cold start может занимать несколько
минут, поэтому healthcheck имеет `start_period=5m`. Следующие старты используют
постоянный docstore и завершают ingestion с `0 changed, N unchanged`.

В `compose.yaml` для сервиса `app` добавлен alias `host.docker.internal:host-gateway`. Он нужен в Linux-сценарии, когда OpenAI-compatible backend работает на хосте, например локальная `Ollama`. Для удалённых backend'ов или контейнерных endpoint'ов эта запись не влияет на работу, если `host.docker.internal` не используется в `OPENAI_BASE_URL`.

4. Проверить сервис:

```bash
docker compose ps
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/docs
```

`/health` всегда отвечает `200`, пока жив процесс FastAPI. `/ready` отвечает `200`, только когда доступен `Redis`; если Redis недоступен, endpoint вернёт `503`.

## Самопроверка контейнеризации

После `docker compose up -d` ожидается:

- `app`, `postgres`, `qdrant` и `redis` работают, healthcheck приложения
  проходит;
- bot запускает Telegram polling и обращается к backend по `http://app:8000`;
- `/ready` возвращает `{"status":"ok","redis":"up"}`;
- startup-log повторного запуска содержит `0 changed, N unchanged`;
- ручные `docker exec` и `pip install` для запуска стека не требуются.

## Быстрый старт через Ollama

1. Поднять `Ollama` и убедиться, что нужная модель уже скачана, например `qwen3`.

2. Поднять `Redis`.

3. Подготовить `.env`:

```env
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
DEFAULT_MODEL=qwen3
VISION_MODEL=qwen2.5vl:7b
LLM_NUM_CTX=8192
REQUEST_TIMEOUT=30
REDIS_URL=redis://host.docker.internal:6379/0
CACHE_TTL_SECONDS=300
LLM_MAX_CONCURRENCY=5
RATE_LIMIT_PER_MIN=30
SECURITY_GUARDRAILS_ENABLED=true
CORS_ORIGINS=[]
LOG_LEVEL=INFO
PHOENIX_TRACING_ENABLED=true
```

4. Поднять FastAPI:

```bash
uv run uvicorn app.main:app --reload
```

Проверка на локальном стенде показала, что `GET /health`, `GET /models`, `POST /chat`, `POST /chat/stream` и кеширование через `Redis` работают с локальным `Ollama`.

## Примеры запросов

```bash
curl http://127.0.0.1:8000/health
```

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

```bash
curl -N -X POST http://127.0.0.1:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"считай до пяти"}]}'
```

```bash
curl http://127.0.0.1:8000/models
```

Чат с серверной историей:

```bash
curl -X POST http://127.0.0.1:8000/chats \
  -H 'Content-Type: application/json' \
  -d '{"owner_external_id":"test-1","interface":"cli"}'
```

```bash
curl -N -X POST http://127.0.0.1:8000/chats/<chat_id>/messages \
  -F 'content=Привет, меня зовут Аня'
```

```bash
curl -N -X POST http://127.0.0.1:8000/chats/<chat_id>/messages \
  -F 'content=Проверь код на изображении' \
  -F 'media=@screenshot.png;type=image/png'
```

```bash
curl 'http://127.0.0.1:8000/chats/<chat_id>/messages?limit=50'
```

```bash
curl -X DELETE http://127.0.0.1:8000/chats/<chat_id>/messages
```

Admin API:

```bash
curl -H 'X-Admin-Token: changeme-admin' \
  http://127.0.0.1:8000/chats/admin/stats
```

```bash
curl -H 'X-Admin-Token: changeme-admin' \
  'http://127.0.0.1:8000/chats/admin/users?limit=50'
```

```bash
curl -X POST http://127.0.0.1:8000/chats/admin/broadcast \
  -H 'X-Admin-Token: changeme-admin' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Плановое уведомление","interface_filter":"telegram"}'
```

Feedback по ответу ассистента:

```bash
curl -X POST \
  http://127.0.0.1:8000/chats/<chat_id>/messages/<message_id>/feedback \
  -H 'Content-Type: application/json' \
  -d '{"value":"up","sources":[{"id":1,"file_name":"ansible.md","page":1,"score":0.73,"snippet":"..."}]}'
```

Подробности по архитектуре, стратегии контекста, Alembic-миграции и переключению
`CHAT_REPOSITORY=json|postgres` описаны в [docs/chat.md](docs/chat.md).

## LiteLLM конфиги

- [docs/litellm/config.production_like.yaml](/workspaces/Review_bot/docs/litellm/config.production_like.yaml:1) — production-like fallback `OpenAI -> Anthropic -> Ollama`
- [docs/litellm/config.yaml](/workspaces/Review_bot/docs/litellm/config.yaml:1) — локальная конфигурация fallback через mock upstream

В обоих конфигах для deployment заданы `rpm` и `tpm`. Эти параметры описывают
плановую пропускную способность backend для LiteLLM Router и не являются
пользовательскими квотами. В текущей FastAPI-реализации есть локальный Redis-backed
HTTP limiter для `/chat` и `/chat/stream`, управляемый `RATE_LIMIT_PER_MIN`;
в production-like контуре внешний API Gateway/nginx всё равно остаётся первым
рубежом защиты публичного HTTP API. Локальный параллелизм FastAPI ограничивается
`LLM_MAX_CONCURRENCY`, а пользовательские LLM-квоты должны задаваться через
LiteLLM virtual keys/teams. Решение и границы ответственности зафиксированы в
[архитектурном паспорте](docs/architecture.md#adr-004-управление-нагрузкой-и-квотами).

## Тесты

```bash
uv run pytest
uv run python -m ruff check app tests scripts
```

Финальный проверенный результат основной suite: `93 passed, 6 skipped`.
RAG-приёмка и фактические score приведены в [docs/rag.md](docs/rag.md).

## Smoke-проверка через uvicorn + curl

Проверка от 2026-06-25 выполнена без mock-компонентов. Использовались
`uvicorn`, `curl`, Redis и OpenAI-compatible backend Ollama.

Конфигурация стенда:

```env
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
DEFAULT_MODEL=qwen3
VISION_MODEL=qwen2.5vl:7b
LLM_NUM_CTX=8192
REQUEST_TIMEOUT=30
REDIS_URL=redis://host.docker.internal:6379/0
SECURITY_GUARDRAILS_ENABLED=true
CHAT_REPOSITORY=json
```

Запуск приложения:

```bash
uv --cache-dir .uv-cache run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Проверенные запросы:

- `GET /health` -> `200`
- `GET /ready` -> `200`, `{"status":"ok","redis":"up"}`
- `GET /models` -> `200`
- первичный `POST /chat` -> `200`, `model=qwen3`, `cached:false`, usage `73/16/89`
- повторный идентичный `POST /chat` -> `200`, `cached:true`
- `POST /chat/stream` с `model=qwen2.5:14b` -> SSE-чанки `1, 2, 3`, затем `usage`, затем `[DONE]`
- `POST /chats` -> `200` и `chat_id`
- `POST /chats/{chat_id}/messages` -> SSE-ответ `pong`, затем JSON-событие `{"type":"done"}`
- `GET /chats/{chat_id}/messages?limit=10` -> сохранённые сообщения пользователя и ассистента

Запрос без серверной истории для проверки кеша:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: live-smoke' \
  -d '{"messages":[{"role":"user","content":"Ответь ровно одним словом: smoke"}],"temperature":0,"max_tokens":16}'
```

Streaming-запрос:

```bash
curl -N -X POST http://127.0.0.1:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: live-smoke-stream' \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Напиши числа 1, 2, 3 через запятую. Только числа."}],"temperature":0,"max_tokens":24}'
```

Запрос к чату с серверной историей:

```bash
curl -N -X POST http://127.0.0.1:8000/chats/<chat_id>/messages \
  -F 'content=/no_think Ответь одним словом: pong'
```

Ограничения локального Ollama-стенда:

- `qwen3` может возвращать reasoning-текст до финального ответа. Текущий API корректно обрабатывает SSE-поток и завершающие события.
- Анализ изображений требует vision-модель в `VISION_MODEL`; текстовая модель может отклонить multimodal-запрос.
- Голосовые сообщения требуют OpenAI-compatible endpoint `/audio/transcriptions` для Whisper. Чистый Ollama endpoint обычно его не предоставляет.
- Ollama не поддерживает `/v1/moderations` в использованной конфигурации. Сервис пишет warning `output_moderation.unavailable` и продолжает обработку в режиме best-effort fallback.
- Если Phoenix UI не запущен на `localhost:6006`, trace exporter пишет предупреждения о недоступном collector и выполняет retry. Для локальной smoke-проверки без Phoenix допустимо установить `PHOENIX_TRACING_ENABLED=false`.
