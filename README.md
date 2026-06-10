# PR Review Assistant HTTP Service

HTTP-сервис на `FastAPI` для транспортного слоя дипломного PR Review Assistant. Сервис поднимает `app.main:app`, принимает запросы на `POST /chat` и `POST /chat/stream`, кеширует обычные ответы в `Redis` и работает с OpenAI-совместимым backend через `AsyncOpenAI`.

На текущем этапе сервис отвечает за HTTP API и интеграцию с LLM backend:

- фронтенд, CLI или IDE-клиент отправляют запрос в `/chat` или `/chat/stream`
- сервис валидирует входные данные, выполняет логирование, читает и записывает кеш, нормализует ошибки
- OpenAI-совместимый backend выполняет генерацию ответа

Сервис не реализует собственную LLM-логику. Его зона ответственности: HTTP-контракт, вызов backend, кеширование и служебные endpoint'ы.

## Что реализовано

- `POST /chat` — обычный completion-ответ с `cached: true/false`
- `POST /chat/stream` — SSE-поток с `data: ...` и финальным `data: [DONE]`
- `GET /health` — liveness без зависимостей
- `GET /ready` — readiness с проверкой `Redis`
- `GET /models` — статический каталог OpenAI-моделей с ценами
- `X-Request-ID`, request logging, CORS и единый формат ошибок

## Архитектура

```text
Client
  -> FastAPI app.main:app
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

## Основные файлы

```text
app/
  main.py
  core/
    config.py
    exceptions.py
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
docs/
  architecture.md
  litellm/
    config.yaml
    config.production_like.yaml
tests/
  test_llm_service.py
```

## Переменные окружения

Шаблон лежит в [.env.example](/workspaces/Review_bot/.env.example:1).

Обязательные ключи приложения:

- `OPENAI_API_KEY`
- `DEFAULT_MODEL`
- `REQUEST_TIMEOUT`
- `REDIS_URL`
- `CACHE_TTL_SECONDS`
- `CORS_ORIGINS`

Дополнительно:

- `OPENAI_BASE_URL` — адрес LiteLLM или другого OpenAI-compatible backend
- `LLM_MAX_CONCURRENCY` — ограничение параллелизма

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
CORS_ORIGINS=[]

OPENAI_UPSTREAM_API_KEY=
ANTHROPIC_API_KEY=
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
OLLAMA_API_KEY=ollama
```

4. Поднять LiteLLM Proxy:

```bash
uv tool install 'litellm[proxy]'
litellm --config docs/litellm/config.production_like.yaml --port 4000
```

5. Поднять FastAPI:

```bash
uv run uvicorn app.main:app --reload
```

6. Открыть Swagger:

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

4. Проверить сервис:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/docs
```

`/health` всегда отвечает `200`, пока жив процесс FastAPI. `/ready` отвечает `200`, только когда доступен `Redis`; если Redis недоступен, endpoint вернёт `503`.

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
docker images llm-service:v1
docker run --rm --entrypoint ls llm-service:v1 -la /app
git ls-files | grep -E '\.env$'
```

Ожидаемый результат:

- `app` и `redis` в статусе `healthy`
- `/health` -> `200` и `{"status":"ok"}`
- `/ready` -> `200` и `{"status":"ok","redis":"up"}`
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
REQUEST_TIMEOUT=30
REDIS_URL=redis://host.docker.internal:6379/0
CACHE_TTL_SECONDS=300
LLM_MAX_CONCURRENCY=5
CORS_ORIGINS=[]
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

## LiteLLM конфиги

- [docs/litellm/config.production_like.yaml](/workspaces/Review_bot/docs/litellm/config.production_like.yaml:1) — production-like fallback `OpenAI -> Anthropic -> Ollama`
- [docs/litellm/config.yaml](/workspaces/Review_bot/docs/litellm/config.yaml:1) — локальная конфигурация fallback через mock upstream

## Тесты

```bash
uv run python -m unittest tests.test_llm_service -v
```

## Проверенный e2e сценарий

На локальном стенде с `Ollama qwen3` и `Redis` проверка показала:

- `GET /health` -> `200`
- `GET /models` -> `200`
- первый `POST /chat` -> `cached: false`
- второй такой же `POST /chat` -> `cached: true`
- `POST /chat/stream` -> SSE-чанки, затем `usage`, затем `[DONE]`

`qwen3` может передавать reasoning-текст до финального ответа. Для текущего API это допустимо: SSE-поток и завершающие события обрабатываются корректно.
