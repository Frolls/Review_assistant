# Модуль чата с серверной историей

## Архитектура

```mermaid
flowchart LR
    Client[Клиент: Telegram / Web / CLI]
    Routes[app/chat/routes.py]
    Service[ChatService]
    Moderation[ModerationService]
    RAG[RAGService]
    Qdrant[(Qdrant corporate_rag)]
    Repo[ChatRepository Protocol]
    Json[(JSONL-файлы)]
    Postgres[(Postgres)]
    LLM[OpenAI-compatible клиент AsyncOpenAI]
    OpenAI[(OpenAI / LiteLLM / Ollama)]

    Client --> Routes
    Routes --> Service
    Service --> Moderation
    Service --> Repo
    Service --> RAG
    Service -->|"media"| LLM
    RAG --> Qdrant
    RAG --> LLM
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
- без `media` сервис передаёт историю в RAG: optional condense улучшает поиск
  короткого follow-up, retrieval выполняется в Qdrant, а score guard может
  вернуть фиксированный отказ без answer-вызова LLM;
- при уверенном retrieval сервис формирует нумерованный контекст, передаёт
  историю генерации и сохраняет sources в metadata сообщения ассистента;
- при наличии вложения либо его контекста в недавней истории используется
  модельный разбор файла без поиска по общей базе;
- сервис накапливает ответ, проверяет его через `ModerationService.check_output()`
  и сохраняет; только после успешной проверки и записи передаёт текст клиенту по SSE.

Сообщения отправляются как `multipart/form-data`: обязательное поле `content`
и опциональный файл `media`. Поддерживаются:

- изображения `image/*` — передаются в LLM как multimodal `image_url`;
- аудио `audio/*` и `application/ogg` — расшифровываются через Whisper-compatible `/audio/transcriptions`;
- PDF — извлекается текст первых 50 страниц, результат ограничивается 30 000 символов;
- DOCX — извлекаются абзацы и таблицы, результат ограничивается 30 000 символов;
- текстовые `.py`, `.yaml`, `.yml`, `.diff`, `.patch`, `.txt`, `.md`, `.json`, `.toml` —
  читаются как UTF-8, бинарное содержимое отклоняется.

Лимит вложения — 10 МиБ, извлечённого текста — 30 000 символов.
Вложение служит контекстом личного диалога и не индексируется в общей базе знаний.

Для изображений можно задать `VISION_MODEL`; если переменная пустая, сервис
использует `DEFAULT_MODEL`. Для локального Ollama voice-сценарий требует
дополнительный OpenAI-compatible STT endpoint, потому что стандартный Ollama
endpoint обычно не реализует `/audio/transcriptions`.

## Модерация

Stateful-чат проверяет пользовательский ввод до LLM-вызова и итоговый ответ
ассистента перед сохранением и показом пользователю. `ModerationService` использует два слоя:

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
- `message_feedback` — оценки ответов ассистента и показанные `sources` с
  `UNIQUE (owner_external_id, message_id)`;
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

Все пользовательские запросы ниже требуют `X-Internal-Token` backend и
`X-User-ID`. Подставьте значения вместо заглушек, а `CHAT_ID` и `MESSAGE_ID`
замените идентификаторами из ответов API. `owner_external_id` при создании
должен совпадать с `X-User-ID`. Пользовательский `system_prompt` не принимается.

Создать или получить чат:

```bash
curl --fail-with-body http://localhost:8000/chats \
  -H 'X-Internal-Token: REPLACE_WITH_INTERNAL_TOKEN' \
  -H 'X-User-ID: test-1' \
  -H 'Content-Type: application/json' \
  -d '{"owner_external_id":"test-1","interface":"cli"}'
```

Получить метаданные чата:

```bash
curl --fail-with-body http://localhost:8000/chats/CHAT_ID \
  -H 'X-Internal-Token: REPLACE_WITH_INTERNAL_TOKEN' \
  -H 'X-User-ID: test-1'
```

Отправить сообщение и получить SSE-поток:

```bash
curl --fail-with-body -N http://localhost:8000/chats/CHAT_ID/messages \
  -H 'X-Internal-Token: REPLACE_WITH_INTERNAL_TOKEN' \
  -H 'X-User-ID: test-1' \
  -F 'content=Как скрыть пароль в Ansible diff?'
```

Отправить файл кода:

```bash
curl --fail-with-body -N http://localhost:8000/chats/CHAT_ID/messages \
  -H 'X-Internal-Token: REPLACE_WITH_INTERNAL_TOKEN' \
  -H 'X-User-ID: test-1' \
  -F 'content=Проверь этот Python-код' \
  -F 'media=@example.py;type=text/plain'
```

SSE возвращает JSON-события после проверки и сохранения полного ответа:

```text
data: {"type":"token","delta":"..."}

data: {"type":"done","message_id":"...","confident":true,"sources":[...]}
```

Переводы строк внутри `delta` экранируются. При отказе либо уточнении
`confident=false`, массив `sources` пуст. SSE здесь не означает показ
непроверенных токенов в момент генерации моделью.

Получить сообщения в хронологическом порядке:

```bash
curl --fail-with-body 'http://localhost:8000/chats/CHAT_ID/messages?limit=50' \
  -H 'X-Internal-Token: REPLACE_WITH_INTERNAL_TOKEN' \
  -H 'X-User-ID: test-1'
```

Очистить историю:

```bash
curl --fail-with-body -X DELETE http://localhost:8000/chats/CHAT_ID/messages \
  -H 'X-Internal-Token: REPLACE_WITH_INTERNAL_TOKEN' \
  -H 'X-User-ID: test-1'
```

Сохранить feedback по ответу ассистента; `sources` содержит фактически
показанные клиентом источники:

```bash
curl --fail-with-body http://localhost:8000/chats/CHAT_ID/messages/MESSAGE_ID/feedback \
  -H 'X-Internal-Token: REPLACE_WITH_INTERNAL_TOKEN' \
  -H 'X-User-ID: test-1' \
  -H 'Content-Type: application/json' \
  -d '{"value":"down","sources":[]}'
```

Административные endpoint проверяют `X-Admin-Token`:

```bash
curl --fail-with-body http://localhost:8000/chats/admin/stats \
  -H 'X-Admin-Token: REPLACE_WITH_ADMIN_TOKEN'
curl --fail-with-body 'http://localhost:8000/chats/admin/users?limit=50' \
  -H 'X-Admin-Token: REPLACE_WITH_ADMIN_TOKEN'
```

`POST /chats/admin/broadcast` с этим ключом ставит рассылку в очередь.
Поля тела: `message` и `interface_filter`. Это действие отправляет сообщения
реальным получателям через бота.

## Историческая smoke-проверка

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
