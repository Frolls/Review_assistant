# Модуль чата с серверной историей

## Архитектура

```mermaid
flowchart LR
    Client[Клиент: Telegram / Web / CLI]
    Routes[app/chat/routes.py]
    Service[ChatService]
    Moderation[ModerationService]
    Repo[ChatRepository Protocol]
    Json[(JSONL-файлы)]
    Postgres[(Postgres)]
    LLM[OpenAI-compatible клиент AsyncOpenAI]
    OpenAI[(OpenAI / LiteLLM / Ollama)]

    Client --> Routes
    Routes --> Service
    Service --> Moderation
    Service --> Repo
    Service --> LLM
    Repo --> Json
    Repo --> Postgres
    LLM --> OpenAI
```

`/chats` — API для диалогов с серверной историей. Сервис хранит метаданные чата
и сообщения. Endpoint'ы `/chat` и `/chat/stream` остаются stateless completion
API: полный контекст передаётся в каждом запросе.

Основной поток работы:

- клиент создаёт чат через `POST /chats`;
- клиент отправляет пользовательское сообщение через `POST /chats/{chat_id}/messages`;
- сервис проверяет вход через `ModerationService.check_input()`;
- сервис при необходимости извлекает данные из `media`, сохраняет сообщение пользователя, формирует контекст, вызывает LLM в streaming-режиме и возвращает SSE-поток;
- после завершения потока сервис проверяет накопленный ответ через `ModerationService.check_output()` и сохраняет ответ ассистента.

Сообщения отправляются как `multipart/form-data`: обязательное поле `content`
и опциональный файл `media`. Поддерживаются:

- изображения `image/*` — передаются в LLM как multimodal `image_url`;
- аудио `audio/*` и `application/ogg` — расшифровываются через Whisper-compatible `/audio/transcriptions`;
- PDF — извлекается текст первых 50 страниц, результат ограничивается 30 000 символов;
- DOCX — извлекаются абзацы и таблицы, результат ограничивается 30 000 символов.

Для изображений можно задать `VISION_MODEL`; если переменная пустая, сервис
использует `DEFAULT_MODEL`. Для локального Ollama voice-сценарий требует
дополнительный OpenAI-compatible STT endpoint, потому что стандартный Ollama
endpoint обычно не реализует `/audio/transcriptions`.

## Модерация

Stateful-чат проверяет пользовательский ввод до LLM-вызова и итоговый ответ
ассистента перед сохранением. `ModerationService` использует два слоя:

- keyword/regex правила из `app/moderation/moderation_keywords.yaml`;
- опциональный OpenAI Moderation API слой с моделью `omni-moderation-latest`,
  включаемый через `MODERATION_OPENAI_ENABLED=true`.

Если вход не проходит модерацию, endpoint возвращает `403`:

```json
{
  "detail": {
    "code": "moderation_blocked",
    "message": "Сообщение заблокировано модерацией.",
    "categories": ["violence"]
  }
}
```

Если не проходит итоговый ответ ассистента, пользователю возвращается безопасный
текст `Не могу показать ответ — он мог нарушить правила`, а в историю
сохраняется уже этот текст.

Инциденты пишутся в structured logs и, для Postgres-репозитория, в таблицу
`moderation_incidents`. Сырой текст не сохраняется: используются короткий
`sha256` hash и PII-masked preview.

## Стратегии контекста

Стратегия по умолчанию — `sliding`. Сервис хранит все сообщения, загружает до
200 последних записей и передаёт в LLM последние `CHAT_CONTEXT_WINDOW`
сообщений, а также `system_prompt` уровня чата, если он задан. Затем
`fit_to_budget()` удаляет старые сообщения с начала списка, сохраняя первое
system-сообщение.

Опциональная стратегия `hybrid` предназначена для длинных диалогов: ревью PR,
поиска по корпоративным материалам или анализа документов. История делится на
две части:

- `old = history[:-KEEP_RECENT]`;
- `recent = history[-KEEP_RECENT:]`.

Старая часть сжимается отдельным LLM-вызовом. Prompt суммаризации требует
перечислить темы, имена, числа, принятые решения и нерешённые вопросы. Итоговый
контекст передаётся в формате `[system_prompt?, system(summary), *recent]`.

Подсчёт токенов использует `tiktoken.get_encoding("o200k_base")` с ChatML
overhead: `+4` токена на сообщение и `+2` токена на весь список. Если локальный
кеш `tiktoken` недоступен, тестовый fallback использует приблизительную оценку
длины текста. Это исключает сетевую зависимость offline-тестов.

## Хранилище

Хранилище задаётся переменными окружения.

JSONL-режим по умолчанию:

```bash
CHAT_REPOSITORY=json
CHAT_STORAGE_DIR=./var/chats
```

Postgres-режим:

```bash
CHAT_REPOSITORY=postgres
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/review_bot
alembic upgrade head
```

В Postgres-режиме дополнительно доступны:

- `moderation_incidents` — журнал заблокированных входов/выходов;
- `message_feedback` — оценки ответов ассистента с `UNIQUE (owner_external_id, message_id)`;
- `broadcast_queue` — очередь admin broadcast-сообщений для Telegram-интерфейса.

Для локальных contract-тестов репозитория укажите `TEST_DATABASE_URL` на
существующую Postgres-базу или используйте временный контейнер `testcontainers`:

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/review_bot_test
uv run pytest tests/chat/test_repository_contract.py
```

Если `TEST_DATABASE_URL` не задан и Docker недоступен, Postgres-часть contract
test suite пропускается. JSON-часть всегда запускается через `tmp_path`.

В JSON-режиме используются следующие файлы:

```text
${CHAT_STORAGE_DIR}/chats/<chat_id>/chat.json
${CHAT_STORAGE_DIR}/chats/<chat_id>/messages.jsonl
```

`DELETE /chats/{chat_id}/messages` в JSONL-режиме добавляет служебную запись
`{"type":"soft_delete","at":"..."}`. В Postgres-режиме для существующих строк
заполняется поле `deleted_at`.

## HTTP endpoint'ы

Создать чат:

```bash
curl -X POST http://localhost:8000/chats \
  -H 'Content-Type: application/json' \
  -d '{"owner_external_id":"test-1","interface":"cli","system_prompt":"Отвечай кратко."}'
```

Получить метаданные чата:

```bash
curl http://localhost:8000/chats/<chat_id>
```

Отправить сообщение и получить SSE-поток:

```bash
curl -N -X POST http://localhost:8000/chats/<chat_id>/messages \
  -F 'content=Привет, меня зовут Аня'
```

Отправить сообщение с файлом:

```bash
curl -N -X POST http://localhost:8000/chats/<chat_id>/messages \
  -F 'content=Проверь код на скриншоте' \
  -F 'media=@screenshot.png;type=image/png'
```

SSE-поток stateful-чата возвращает JSON-события:

```text
data: {"type":"token","delta":"..."}

data: {"type":"done","message_id":"..."}
```

Получить сообщения в хронологическом порядке:

```bash
curl 'http://localhost:8000/chats/<chat_id>/messages?limit=50'
```

Очистить историю сообщений:

```bash
curl -X DELETE http://localhost:8000/chats/<chat_id>/messages
```

Сохранить feedback по ответу ассистента:

```bash
curl -X POST http://localhost:8000/chats/<chat_id>/messages/<message_id>/feedback \
  -H 'Content-Type: application/json' \
  -d '{"value":"down"}'
```

Admin endpoint'ы защищены заголовком `X-Admin-Token`:

```bash
curl -H 'X-Admin-Token: changeme-admin' http://localhost:8000/chats/admin/stats
curl -H 'X-Admin-Token: changeme-admin' 'http://localhost:8000/chats/admin/users?limit=50'
curl -X POST http://localhost:8000/chats/admin/broadcast \
  -H 'X-Admin-Token: changeme-admin' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Плановое уведомление","interface_filter":"telegram"}'
```

## Smoke-проверка

Условия проверки от 2026-06-25: `uvicorn`, `curl`, Redis и OpenAI-compatible
backend Ollama. Результаты:

- `POST /chats` создал чат и вернул `chat_id`;
- `POST /chats/{chat_id}/messages` отдал SSE-ответ `pong` и финальное
  JSON-событие `{"type":"done"}`;
- `GET /chats/{chat_id}/messages?limit=10` вернул сохранённые сообщения
  пользователя и ассистента;
- `GET /chats/{chat_id}` вернул метаданные чата.

Конфигурация:

```env
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
DEFAULT_MODEL=qwen3
VISION_MODEL=qwen2.5vl:7b
LLM_NUM_CTX=8192
REDIS_URL=redis://host.docker.internal:6379/0
CHAT_REPOSITORY=json
```

Если Phoenix UI не запущен на `localhost:6006`, trace exporter пишет
предупреждения о недоступном collector и выполняет retry. HTTP-ответы остаются
успешными, но время выполнения может увеличиться. Для локальной smoke-проверки
без Phoenix допустимо установить `PHOENIX_TRACING_ENABLED=false`.
