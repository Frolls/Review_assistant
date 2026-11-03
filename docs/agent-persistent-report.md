> Исторический отчёт этапа курса. Итоговая архитектура и отчёт исправлений
> прилагаются автором отдельно в материалах защиты. Роль `full` больше
> не доступна через API, подтверждения связаны с владельцем и уникальным действием.

# Persistent ReAct-агент: checkpoints, HIL, SSE и time travel

## 1. Backend checkpoint’ов в разных режимах

Backend выбирается через
`AGENT_CHECKPOINTER=memory|sqlite|postgres`.

- `sqlite` — локальный backend по умолчанию. Файл задаётся через
  `AGENT_SQLITE_PATH` (по умолчанию
  `./var/agent_checkpoints.sqlite`), поэтому локальный рестарт FastAPI не
  теряет state.
- `postgres` включается для `app` в Compose. Checkpoint’ы переживают
  замену контейнера и доступны нескольким worker’ам.
- `memory` — явно включаемый одноразовый режим для узких тестов и
  экспериментов.

`agent_lifespan()` открывает выбранный saver и один раз вызывает
`await checkpointer.setup()` до передачи скомпилированного графа.
HTTP-handler’ы берут готовый граф из `app.state.agent_graph` и никогда не
вызывают `setup()`. На 2026-08-08 разрешились
`langgraph-checkpoint-sqlite==3.1.1` и
`langgraph-checkpoint-postgres==3.1.2`; в `pyproject.toml` зафиксирована
совместимая линейка `>=3.1,<4`. `aiosqlite` и psycopg v3 с extras
`binary,pool` указаны как прямые зависимости.

## 2. PostgreSQL в Docker Compose

Новый сервис или отдельная БД не создавались. LangGraph использует
существующий PostgreSQL 16 и существующую БД `review_bot`. Приложение
получает те же значения через `POSTGRES_HOST`, `POSTGRES_PORT`,
`POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD`; saver строит из них
обычный psycopg v3 URI `postgresql://`. SQLAlchemy по-прежнему использует
`postgresql+asyncpg://` из `DATABASE_URL`.

После старта FastAPI выполнена проверка:

```console
$ docker compose exec -T postgres psql -U postgres -d review_bot -c '\dt checkpoint*'
                 List of relations
 Schema |         Name          | Type  |  Owner
--------+-----------------------+-------+----------
 public | checkpoint_blobs      | table | postgres
 public | checkpoint_migrations | table | postgres
 public | checkpoint_writes     | table | postgres
 public | checkpoints           | table | postgres
(4 rows)
```

Thread `demo-2` был оставлен на interrupt, после чего контейнер `app`
пересоздан. `POST /agent/stream` с тем же `thread_id` и
`"resume":false` продолжил работу с
`confirm_and_execute_telegram_message` и завершился с `sent=false`. Так
проверено восстановление после реальной замены контейнера, а не только
наличие строк в БД.

`include_name` исключает из Alembic все четыре таблицы. Их схемой управляет
LangGraph `setup()`, а доменными таблицами — Alembic.

## 3. Опасное действие и граница идемпотентности

Опасный tool — `send_telegram_message`. Он делает аутентифицированный HTTP
`POST /notify` в backchannel Telegram-бота, поэтому ошибочный или повторный вызов
приводит к видимому внешнему эффекту.

В графе есть два узла и явное ребро:

```text
prepare_telegram_message -> confirm_and_execute_telegram_message
```

**До `interrupt`:** `prepare_telegram_message` читает tool call модели,
валидирует и нормализует `chat_id` и текст, создаёт случайный `uuid4().hex`
в качестве `request_id`, формирует preview и сохраняет draft в checkpoint.
Resume использует уже сохранённый draft; внешний эффект на этом шаге отсутствует.

**После `interrupt`:** только положительный resume с актуальным `request_id`
вызывает `notify_user`, где выполняется HTTP side effect. Отказ записывает
`sent=false` и `ToolMessage`, не вызывая sender. Это разделение важно,
потому что при resume LangGraph запускает interrupt-узел с начала. HTTP-вызов
до `interrupt()` привёл бы к двойной отправке.

## 4. Лог HIL

Офлайн-демо дошло до dynamic interrupt с payload:

```json
{
  "type": "approve_telegram_message",
  "preview": "Telegram → 4242: PR #42 готов к review",
  "request_id": "demo-pr-42"
}
```

После `await graph.ainvoke(Command(resume=True), config)` получено:

```json
{
  "thread_id": "demo-approve-pr-42",
  "decision": true,
  "sent": true,
  "external_calls": [[4242, "PR #42 готов к review"]]
}
```

В smoke-тестах sender заменён на `AsyncMock`. Для одобрения проверяется
один await, для отказа — `assert_not_called()`.
Дополнительно полный suite в Docker-окружении завершился с результатом
`127 passed, 6 skipped`.

## 5. Time travel и альтернативные решения

`python scripts/time_travel_demo.py` использует SQLite in-memory и не
требует LLM или сети. История checkpoint’ов (от новых к старым):

```text
checkpoint_id                           | next
----------------------------------------+----------------------------------------
1f193489-d6d3-6c12-8002-37757ef48332    | confirm_and_execute_telegram_message
1f193489-d6d3-6226-8001-1957d3ff67ca    | prepare_telegram_message
1f193489-d6d1-6673-8000-8776da92a13e    | call_model
1f193489-d6cf-6d16-bfff-58ce9257c037    | __start__
```

Чтение первого ID через
`aget_state({"configurable": {"thread_id": ..., "checkpoint_id": ...}})` вернуло
готовый draft, `sent=false` и
`next=("confirm_and_execute_telegram_message",)`.

Противоположные исходы получены из одинакового input в двух lineage:

```json
{
  "rejected": {"thread_id": "demo-reject-pr-42", "decision": false, "sent": false},
  "approved": {"thread_id": "demo-approve-pr-42", "decision": true, "sent": true}
}
```

Это сделано намеренно: `Command(resume=...)` хранится как pending write,
поэтому первое доставленное решение детерминировано для lineage.
Повторный resume одного interrupt-checkpoint’а с другим Boolean не создаёт
ветку. Для ветвления нужны разные стабильные `thread_id`, как в демо, либо
явный fork через `update_state`.

## 6. Streaming mode и curl-проверка

Endpoint использует `graph.astream(..., stream_mode=["updates"])`.
Это события завершённых узлов, а не поток необработанных токенов модели.
Во время ожидания сервер отправляет безопасный `progress` каждые 10 секунд;
Telegram обновляет текст состояния по мере завершения узлов. Финальный ответ
публикуется после модерации. На SSE-ход действует общий deadline 180 секунд.
Исторические curl-фрагменты ниже иллюстрируют форму запроса; текущий API
дополнительно требует `X-Internal-Token`, `X-User-ID` и `request_id` при resume.

```console
$ curl -N -X POST http://localhost:8000/agent/stream \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"demo-2","user_role":"write-with-approve","input":{"messages":[{"role":"user","content":"Отправь в Telegram-чат 4242 сообщение: PR #42 готов к review."}]}}'
data: {"event":"start","data":{"thread_id":"demo-2"}}
data: {"event":"updates","data":{"prepare_telegram_message":{"sent":false,...}}}
data: {"event":"interrupt","data":{"__interrupt__":[{"value":{"type":"approve_telegram_message","preview":"Telegram → 4242: PR #42 готов к review.",...}}]}}
data: {"event":"paused","data":{"next":["confirm_and_execute_telegram_message"],...}}
```

Тот же стабильный thread продолжается так:

```console
$ curl -N -X POST http://localhost:8000/agent/stream \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"demo-2","user_role":"write-with-approve","resume":false}'
```

Для одобрения передаётся `"resume":true`; router создаёт
`Command(resume=True)`. Ответ имеет `text/event-stream`, `Cache-Control: no-cache` и
`X-Accel-Buffering: no`.

Успешная ветка проверена end-to-end с контролируемым HTTP mock
backchannel’а. После паузы thread `criteria-approve-1` получил
`"resume":true`:

```text
data: {"event":"updates","data":{"confirm_and_execute_telegram_message":{
  "decision":true,"sent":true,
  "delivery_result":"Telegram message sent to 4242."}}}
data: {"event":"messages","data":{"content":"Сообщение успешно...","node":"call_model"}}
data: {"event":"done","data":{"next":[]}}
```

Mock-сервер зафиксировал реальный side effect через HTTP:

```text
NOTIFY path=/notify body={"chat_id":4242,"text":"проверка resume true"}
```

## 7. Permission policy

API принимает только `read-only` и `write-with-approve`. Первый режим
отключает отправку; второй требует interrupt и подтверждение актуального
`request_id`. Роль `full` отклоняется схемой. Получатель ограничен личным
чатом инициатора. Сервер связывает клиентский `thread_id` с удостоверенным
владельцем через SHA-256; Telegram использует `telegram-actions`.

`/clear_agent` удаляет checkpoint-поток и pending action; старые кнопки
становятся недействительны, в том числе после рестарта. `/clear` дополнительно
очищает обычный диалог, `/clear_chat` оставляет память агента. Очищение
не удаляет журнал уже выполненных или неопределённых доставок.
Контекст ограничен десятью полными пользовательскими ходами и 24 000 символами.
Tool-call и ToolMessage удаляются вместе. Текущий незавершённый ход сохраняется;
если он сам превышает лимит, новый вызов модели отклоняется.

## 8. Что осталось хрупким

После исправления 3 ноября результат `search_knowledge_base` публикуется как
точная выдержка первого найденного источника с его номером и именем. Второй
LLM-вызов для свободного объяснения этой выдержки не выполняется: в live-проверке
он добавлял ошибочное утверждение об `Optional[T]`. Это извлечение и атрибуция
источника, а не отдельная семантическая проверка всего корпуса. Следующий
пользовательский ход и остальные инструменты работают через обычный цикл.
Актуальный повтор: [live-проверка v12](verification/README.md).

- Backchannel принимает ключ, полученный из thread ID и сохранённого request ID.
  SQLite-журнал связывает ключ с хешем адресата/текста и состоянием доставки.
  Повтор завершённой доставки возвращает прежний результат; другой payload
  или неопределённый исход не вызывает слепой повтор. Неопределённый исход
  требует проверки адресатом: Telegram API не предоставляет exactly-once.
- В SSE ещё нет буфера повтора по `Last-Event-ID` и политики отмены при
  отключении клиента. Persistent state позволяет переподключиться и сделать
  resume, но контракт transport-level replay ещё нужно спроектировать.
- Postgres saver проверен с одним Uvicorn worker. До горизонтального
  масштабирования нужны load-тест и тест concurrent resume.
