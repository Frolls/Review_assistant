# AI PR Review Assistant HTTP Service

HTTP-сервис на `FastAPI` для дипломного проекта «ИИ-ассистент для ревью кода». Цель ассистента — улучшать качество кода и сокращать время ревью pull request'ов. В качестве источников рекомендаций используются Python Enhancement Proposals (PEP), Ansible community documentation, внутренние руководства по стилю кода и архитектурные документы.

Сервис поднимает `app.main:app`, принимает запросы на `POST /chat`, `POST /chat/stream` и stateful API `/chats`, кеширует обычные ответы в `Redis`, работает с OpenAI-совместимым backend через `AsyncOpenAI` и экспортирует trace/span-данные в Phoenix.

На текущем этапе сервис отвечает за HTTP API, eval/testing слой и интеграцию с LLM backend:

- фронтенд, CLI или IDE-клиент отправляют вопросы по ревью PR в `/chat` или `/chat/stream`
- сервис валидирует входные данные, применяет защитный слой, выполняет логирование, читает и записывает кеш, нормализует ошибки
- OpenAI-совместимый backend выполняет генерацию ответа

Сервис не реализует собственную модель. Его зона ответственности: HTTP-контракт, вызов backend, кеширование, служебные endpoint'ы и инфраструктура проверки качества ответов.

## Что реализовано

- `POST /chat` — обычный completion-ответ с `cached: true/false`
- `POST /chat/stream` — SSE-поток с `data: ...` и финальным `data: [DONE]`
- `POST /chats` и `/chats/{chat_id}/messages` — stateful-чат с серверной историей, SSE-ответом и JSON/Postgres-хранилищем
- `POST /chats/{chat_id}/messages` принимает `multipart/form-data`: поле `content` и опциональный файл `media` для изображений, аудио, PDF и DOCX
- `GET /health` — liveness без зависимостей
- `GET /ready` — readiness с проверкой `Redis`
- `GET /models` — статический каталог OpenAI-моделей с ценами
- Redis-backed rate limiting для `/chat` и `/chat/stream` по `X-User-ID` или IP
- защитный слой для `/chat`: prompt-injection validator, canary-token в system prompt, output filter, PII masking и best-effort moderation
- `X-Request-ID`, request logging, CORS и единый формат ошибок
- structured JSON logs с `request_id`, latency, token usage, finish reason, `prompt_hash` и безопасным `prompt_preview`
- OpenInference/Phoenix tracing для `/chat` и LLM-вызовов с `gen_ai.*` атрибутами
- PII-redaction для email, российских телефонов, карт, ИНН и паспортов перед записью prompt/output preview в логи
- быстрый unit testing layer вокруг LLM-adjacent логики и отдельный offline evaluation layer в `eval/`
- security evaluation layer на базе NVIDIA garak с baseline/after отчётами

## Архитектура

```text
Client
  -> FastAPI app.main:app
  -> Phoenix collector/UI
  -> OpenAI-compatible backend
     -> OpenAI API
     -> LiteLLM Proxy
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
    health.py
    models.py
  schemas/
    chat.py
    models.py
  services/
    llm.py
    security/
      input_validator.py
      output_filter.py
  chat/
    domain.py
    repository.py
    service.py
    routes.py
    deps.py
    repositories/
docs/
  chat.md
  architecture.md
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
  load_test.py
tests/
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

Trace Phoenix хранятся в docker volume `phoenix-data` в базе `/data/phoenix.db`.
Structured logs сохраняются в stdout контейнера `app`. Кеш ответов хранится
отдельно в `Redis`.

Подробное описание конфигурации и ожидаемого поведения приведено в
`docs/observability/README.md`.

## Testing и evaluation

Быстрые unit-тесты запускаются без API-ключей и без сети:

```bash
uv run pytest tests/unit/ -m "not llm"
```

Evaluation живёт отдельно от `tests/`, потому что это медленный ручной прогон с production model и judge model:

```bash
uv run python eval/run_evaluation.py --golden eval/golden_dataset.json --judge gpt-5.2 --out eval/runs/$(date +%F).json
uv run python eval/check_thresholds.py
```

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

4. При необходимости поднять Phoenix UI для локального просмотра trace:

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

1. Подготовить `.env`:

```bash
cp .env.example .env
```

2. Заполнить в `.env` как минимум `OPENAI_API_KEY` и при необходимости `OPENAI_BASE_URL`.

3. Поднять стек:

```bash
docker compose up -d --build
```

Команда поднимет три сервиса: `app`, `redis`, `phoenix`.

В `compose.yaml` для сервиса `app` добавлен alias `host.docker.internal:host-gateway`. Он нужен в Linux-сценарии, когда OpenAI-compatible backend работает на хосте, например локальная `Ollama`. Для удалённых backend'ов или контейнерных endpoint'ов эта запись не влияет на работу, если `host.docker.internal` не используется в `OPENAI_BASE_URL`.

4. Проверить сервис:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/docs
```

`/health` всегда отвечает `200`, пока жив процесс FastAPI. `/ready` отвечает `200`, только когда доступен `Redis`; если Redis недоступен, endpoint вернёт `503`.

Phoenix UI после старта доступен на `http://127.0.0.1:6006`. Для появления trace достаточно выполнить любой запрос в `/chat` или `/chat/stream`.

`compose.override.yaml` используется для локальной разработки: при обычном `docker compose up` он включает `uvicorn --reload` и bind mount только для каталога `app/`, чтобы изменения Python-кода подхватывались без пересборки образа.

## Самопроверка контейнеризации

После `docker compose up -d --build` можно проверить стек так:

```bash
docker compose ps
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
curl -s http://127.0.0.1:8000/docs > /dev/null
docker compose exec -T app id
docker compose exec -T redis redis-cli ping
curl -s http://127.0.0.1:6006 > /dev/null
docker images llm-service:v1
docker run --rm --entrypoint ls llm-service:v1 -la /app
git ls-files | grep -E '\.env$'
```

Ожидаемый результат:

- `app` и `redis` в статусе `healthy`
- `phoenix` в статусе `running`
- `/health` -> `200` и `{"status":"ok"}`
- `/ready` -> `200` и `{"status":"ok","redis":"up"}`
- UI Phoenix открывается на `http://127.0.0.1:6006`
- `docker compose exec -T app id` показывает `uid=1000(appuser)`
- `docker compose exec -T redis redis-cli ping` возвращает `PONG`
- в `/app` внутри образа нет `.env`, `.git`, `tests/`
- `git ls-files | grep -E '\.env$'` ничего не выводит

Проверка на локальной машине показала:

- `docker compose up -d --build` поднимает стек одной командой
- размер итогового образа `llm-service:v1` — `163 MB`
- при остановке `redis` endpoint `/health` остаётся `200`, а `/ready` становится `503`

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
