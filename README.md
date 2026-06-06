# PR Review Assistant HTTP Service

HTTP-сервис на `FastAPI`, который является ядром дипломного **review-ассистента для PR и code review**. Он поднимает `app.main:app`, принимает запросы на `POST /chat` и `POST /chat/stream`, кеширует обычные ответы в `Redis` и может работать как с OpenAI напрямую, так и через **LiteLLM Proxy** по OpenAI-совместимому API.

На текущем этапе это именно транспортный и orchestration-слой review-ассистента:

- фронтенд, CLI или IDE-клиент отправляют вопрос по ревью в `/chat` или `/chat/stream`
- сервис валидирует запрос, добавляет observability, кеш и единый формат ошибок
- OpenAI-compatible backend отвечает уже за генерацию review-ответа

То есть речь не про абстрактный “чат-сервис”, а про HTTP-обёртку вокруг LLM-слоя дипломного PR Review Assistant.

## Что реализовано

- `POST /chat` — обычный completion-ответ с `cached: true/false`
- `POST /chat/stream` — SSE-поток с `data: ...` и финальным `data: [DONE]`
- `GET /health` — liveness без зависимостей
- `GET /models` — статический каталог OpenAI-моделей с ценами
- `X-Request-ID`, request logging, CORS и единый формат ошибок

## Архитектура

```text
Client
  -> FastAPI app.main:app
  -> OpenAI-compatible backend
     -> OpenAI напрямую
     -> или LiteLLM Proxy
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

- `OPENAI_BASE_URL` — полезен для LiteLLM или другого OpenAI-compatible backend
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

Этот сценарий был проверен живьём: `GET /health`, `GET /models`, `POST /chat`, `POST /chat/stream` и Redis cache hit работают на локальном `Ollama + Redis`.

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
- [docs/litellm/config.yaml](/workspaces/Review_bot/docs/litellm/config.yaml:1) — локальный demo fallback через mock upstream

## Тесты

```bash
uv run python -m unittest tests.test_llm_service -v
```

## Проверенный e2e сценарий

На локальном стенде с `Ollama qwen3` и `Redis` сервис показал:

- `GET /health` -> `200`
- `GET /models` -> `200`
- первый `POST /chat` -> `cached: false`
- второй такой же `POST /chat` -> `cached: true` и заметно быстрее
- `POST /chat/stream` -> SSE-чанки, затем `usage`, затем `[DONE]`

Нюанс: `qwen3` может стримить reasoning-текст перед финальным ответом. Для текущего ТЗ это не мешает, потому что протокол SSE и финальные события работают корректно.
