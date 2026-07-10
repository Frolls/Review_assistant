# Qdrant Vector Store

## Назначение

Qdrant используется как vector store для RAG-слоя PR Review Assistant. Индекс хранит фрагменты базы знаний по code review, security review, Python style, Ansible, PostgreSQL migrations, API contracts, observability и эксплуатационным runbook'ам.

Источник данных: `data/review_knowledge.json`.

Формат источника:

```json
{
  "documents": [
    {
      "source": "secure_code_review.md",
      "title": "Secure Code Review",
      "category": "security",
      "department": "platform",
      "tenant_id": "core",
      "access_level": "internal",
      "archived": false,
      "created_at": "2026-07-01",
      "chunks": ["..."]
    }
  ]
}
```

Загрузчик валидирует обязательные поля до embedding/upsert. Для каждого chunk формируется payload с исходной metadata, `text`, `chunk_index` и `document_id`.

## Запуск

```bash
docker compose up -d qdrant
python scripts/load_to_qdrant.py
```

Dashboard: `http://localhost:6333/dashboard`.

Переменные окружения:

```bash
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=dev-qdrant-key
QDRANT_COLLECTION=documents
EMBEDDING_DIM=2560
```

В `compose.yaml` приложение получает `QDRANT_URL=http://qdrant:6333`. В коде сервиса адрес Qdrant не хардкодится.

## Коллекция

Коллекция: `documents`.

Параметры:

- vector size: `2560`
- distance: `COSINE`
- HNSW: `m=16`, `ef_construct=100`
- загружено: `140` points

Если существующая коллекция имеет размерность, отличную от `EMBEDDING_DIM`, `ensure_collection()` завершает запуск с ошибкой. Это предотвращает запись embeddings от другой модели в существующую коллекцию.

Payload indexes:

- `source`: `KEYWORD`
- `created_at`: `DATETIME`
- `tenant_id`: `KEYWORD`
- `category`: `KEYWORD`
- `access_level`: `KEYWORD`
- `archived`: `KEYWORD`

## Загрузка

`scripts/load_to_qdrant.py` выполняет следующие операции:

1. Создаёт коллекцию `documents`, если она отсутствует.
2. Проверяет размерность существующей коллекции.
3. Создаёт payload indexes.
4. Загружает документы из `data/review_knowledge.json`.
5. Строит embeddings через `app.services.embeddings`.
6. Проверяет, что размерность каждого vector равна `EMBEDDING_DIM`.
7. Выполняет batch upsert через `VectorStore`.
8. Печатает `points_count`.

Идентификатор точки детерминированный:

```text
uuid5(NAMESPACE_URL, source + category + chunk_index + text)
```

Повторный запуск обновляет существующие точки и не создаёт дубли. Проверенный результат после повторной загрузки: `points_count=140`.

## FastAPI Integration

`app.main.lifespan` создаёт один экземпляр `VectorStore` на процесс приложения:

- `AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)`
- `await vector_store.ensure_collection()`
- `app.state.vector_store = vector_store`
- `await vector_store.close()` при shutdown

Клиент Qdrant не создаётся на каждый запрос.

## Метрика

Боевая коллекция использует `COSINE`.

Причины:

- embedding-сервис нормализует vectors для OpenAI-compatible embeddings и `sentence-transformers`;
- при нормализованных vectors `COSINE` и `DOT` дают одинаковое ранжирование;
- `COSINE` сохраняет корректную семантику similarity при смене provider, если будущий provider не нормализует vectors.

Проверка выполняется командой:

```bash
python scripts/load_to_qdrant.py --compare-metrics
```

Скрипт создаёт временные коллекции `documents_cosine` и `documents_dot`, загружает одинаковые points, выполняет пять запросов и удаляет временные коллекции.

Фактический результат:

| Запрос | top-5 ids в COSINE | top-5 ids в DOT | Совпало |
| --- | --- | --- | --- |
| Как проверить безопасность PR с внешними URL и токенами? | `secure_code_review.md#4`, `secure_code_review.md#0`, `secure_code_review.md#6`, `secure_code_review.md#5`, `platform_private_runbook.md#6` | `secure_code_review.md#4`, `secure_code_review.md#0`, `secure_code_review.md#6`, `secure_code_review.md#5`, `platform_private_runbook.md#6` | да |
| Какие правила использовать для Ansible idempotency? | `ansible_best_practices.md#0`, `ansible_best_practices.md#9`, `ansible_best_practices.md#1`, `ansible_best_practices.md#8`, `ansible_best_practices.md#4` | `ansible_best_practices.md#0`, `ansible_best_practices.md#9`, `ansible_best_practices.md#1`, `ansible_best_practices.md#8`, `ansible_best_practices.md#4` | да |
| Что делать с flaky тестами в pull request? | `retrieval_eval_dataset.md#6`, `pep8_style_guide.md#9`, `secure_code_review.md#4`, `pep8_style_guide.md#6`, `retrieval_eval_dataset.md#9` | `retrieval_eval_dataset.md#6`, `pep8_style_guide.md#9`, `secure_code_review.md#4`, `pep8_style_guide.md#6`, `retrieval_eval_dataset.md#9` | да |
| Как ревьюить миграцию Postgres без долгих блокировок? | `postgres_migration_checklist.md#0`, `postgres_migration_checklist.md#6`, `postgres_migration_checklist.md#9`, `postgres_migration_checklist.md#2`, `postgres_migration_checklist.md#8` | `postgres_migration_checklist.md#0`, `postgres_migration_checklist.md#6`, `postgres_migration_checklist.md#9`, `postgres_migration_checklist.md#2`, `postgres_migration_checklist.md#8` | да |
| Какие документы доступны только platform team? | `platform_private_runbook.md#0`, `platform_private_runbook.md#7`, `platform_private_runbook.md#6`, `secure_code_review.md#0`, `archived_legacy_index.md#8` | `platform_private_runbook.md#0`, `platform_private_runbook.md#7`, `platform_private_runbook.md#6`, `secure_code_review.md#0`, `archived_legacy_index.md#8` | да |

После проверки в Qdrant остаётся только коллекция `documents`.

## Фильтры

### Match по строке

Фильтр:

```python
from qdrant_client.http.models import FieldCondition, Filter, MatchValue

query_filter = Filter(
    must=[
        FieldCondition(
            key="category",
            match=MatchValue(value="security"),
        )
    ]
)
```

Запрос: `как не утечь секретами в PR`.

Top-3:

| id | source | category |
| --- | --- | --- |
| `secure_code_review.md#0` | `secure_code_review.md` | `security` |
| `secure_code_review.md#4` | `secure_code_review.md` | `security` |
| `platform_private_runbook.md#6` | `platform_private_runbook.md` | `security` |

### Range по дате

Дата запуска проверки: `2026-07-10`.

Фильтр за последние 30 дней:

```python
from qdrant_client.http.models import DatetimeRange, FieldCondition, Filter

query_filter = Filter(
    must=[
        FieldCondition(
            key="created_at",
            range=DatetimeRange(gte="2026-06-10T00:00:00Z"),
        )
    ]
)
```

Запрос: `как настроить RAG retrieval`.

Без фильтра в top-3 попадают документы:

- `retrieval_eval_dataset.md#9`, `created_at=2026-04-28T00:00:00Z`
- `rag_retrieval_policy.md#0`, `created_at=2026-07-08T00:00:00Z`
- `review_ui_notes.md#0`, `created_at=2026-03-18T00:00:00Z`

С фильтром:

| id | source | created_at |
| --- | --- | --- |
| `rag_retrieval_policy.md#0` | `rag_retrieval_policy.md` | `2026-07-08T00:00:00Z` |
| `rag_retrieval_policy.md#5` | `rag_retrieval_policy.md` | `2026-07-08T00:00:00Z` |
| `platform_private_runbook.md#3` | `platform_private_runbook.md` | `2026-07-09T00:00:00Z` |

### must + must_not

Фильтр:

```python
from qdrant_client.http.models import FieldCondition, Filter, MatchValue

query_filter = Filter(
    must=[
        FieldCondition(key="tenant_id", match=MatchValue(value="core")),
    ],
    must_not=[
        FieldCondition(key="archived", match=MatchValue(value="true")),
    ],
)
```

Запрос: `как проверять типизацию и стиль Python в PR`.

Top-3:

| id | source | tenant_id | archived |
| --- | --- | --- | --- |
| `pep8_style_guide.md#6` | `pep8_style_guide.md` | `core` | `false` |
| `pep8_style_guide.md#9` | `pep8_style_guide.md` | `core` | `false` |
| `pep484_typing.md#9` | `pep484_typing.md` | `core` | `false` |
