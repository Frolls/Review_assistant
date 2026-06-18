# Observability

`phoenix-trace.png` содержит пример trace из Phoenix UI для проекта
`ai-pr-review-assistant`.

## Назначение

Документ описывает:

- состав observability-данных, формируемых приложением;
- правила экспорта trace в Phoenix;
- режимы скрытия чувствительных данных;
- физическое размещение trace, логов и кеша.

## Компоненты

- `Phoenix UI` доступен по адресу `http://127.0.0.1:6006`.
- приложение экспортирует trace в Phoenix по OTLP HTTP;
- имя проекта в Phoenix задаётся переменной `PHOENIX_PROJECT_NAME`.

Значение по умолчанию для `PHOENIX_PROJECT_NAME`: `ai-pr-review-assistant`.

## Состав trace

При обработке одного запроса `POST /chat` формируются как минимум два span:

- `chat.request` — прикладной span сервиса;
- `ChatCompletion` — span, создаваемый auto-instrumentation OpenInference для вызова LLM backend.

### Span `chat.request`

Основные атрибуты span:

- `llm.prompt_hash`;
- `llm.prompt_preview`;
- `llm.prompt_length`;
- `llm.output_preview`;
- `llm.output_length`;
- `llm.cache_status`;
- `llm.latency_ms`;
- `gen_ai.request.model`;
- `gen_ai.response.model`;
- `gen_ai.usage.input_tokens`;
- `gen_ai.usage.output_tokens`;
- `gen_ai.usage.total_tokens`.

### Span `ChatCompletion`

Основные данные span:

- статус выполнения;
- latency;
- идентификатор модели;
- usage-метрики;
- invocation parameters.

## Режим скрытия содержимого

Рекомендуемая конфигурация для production-like среды:

```env
OBSERVABILITY_INCLUDE_CONTENT=false
```

При `OBSERVABILITY_INCLUDE_CONTENT=false` применяются следующие правила:

- `chat.request` не сохраняет сырой prompt и сырой response в `input.value` и `output.value`;
- вместо исходного содержимого в span сохраняются безопасные атрибуты, включая `llm.prompt_hash` и `llm.prompt_preview`;
- `ChatCompletion` скрывает сырой `LLM Input` и `LLM Output` на уровне OpenInference `TraceConfig`.

Ожидаемое отображение в Phoenix:

- для `chat.request` значения `input.value` и `output.value` равны `[redacted]`;
- для `ChatCompletion` блоки `LLM Input` и `LLM Output` отображаются как `__REDACTED__`.

Для локальной отладки допускается временно включить полный контент:

```env
OBSERVABILITY_INCLUDE_CONTENT=true
```

После изменения значения требуется перезапуск `app`.

## Маскирование PII в логах

Structured logs не сохраняют исходный prompt целиком. Вместо этого используются:

- `prompt_hash` — короткий стабильный digest;
- `prompt_preview` — редактированное preview prompt.

Regex-маскирование покрывает следующие типы данных:

- `EMAIL`;
- `PHONE_RU`;
- `CARD`;
- `INN`;
- `PASSPORT`.

Для длинных prompt дополнительно запускается фоновая anonymization имён через
Presidio и spaCy. Результат фиксируется событием `pii_redaction_completed`.

Примечания:

- редактирование `prompt_preview` относится только к structured logs;
- скрытие `LLM Input` и `LLM Output` в Phoenix конфигурируется отдельно через OpenInference `TraceConfig`.

## Хранение данных

- trace Phoenix хранятся в docker volume `phoenix-data`;
- внутри контейнера Phoenix база данных расположена по пути `/data/phoenix.db`;
- structured logs сохраняются в stdout контейнера `app`;
- кеш ответов хранится в `Redis`.

Trace, записанные до включения безопасного режима, могут содержать сырой
контент. Изменение конфигурации влияет только на новые trace.

## Проверка конфигурации

Для безопасного режима нормальным считается следующее поведение:

- `chat.request` отображается как отдельный root span;
- `ChatCompletion` отображается как LLM span;
- `LLM Input` и `LLM Output` в `ChatCompletion` имеют значение `__REDACTED__`;
- в `chat.request` доступны безопасные preview-атрибуты и usage-метрики.
