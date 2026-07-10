# Embeddings для RAG-ассистента

## Выбор модели

RAG-слой использует Qdrant-коллекцию `documents`. Embedding-сервис формирует
vectors для документов из `data/review_knowledge.json` и пользовательских
retrieval-запросов. Корпус технический и смешанный по языку: правила code
review, Ansible, PostgreSQL, API contracts и internal runbooks содержат русский
текст, английские идентификаторы, параметры окружения, пути файлов и фрагменты
кода. Модель выбиралась как retrieval-инструмент для русско-английского корпуса
с кодовыми терминами.

Базовая модель для дипломного RAG-индекса: `qwen3-embedding:4b` через Ollama.
Она уже загружена локально, доступна через OpenAI-compatible `/v1/embeddings` и
возвращает 2560-мерные векторы.

Обоснование выбора:

- язык корпуса: документы проекта написаны в основном на русском, но содержат
  английские идентификаторы, названия API, параметры окружения и фрагменты кода;
  семейство Qwen3 Embedding рассчитано на multilingual retrieval и code
  retrieval, поэтому соответствует предметной области проекта;
- размерность: локально проверенная `qwen3-embedding:4b` в Ollama возвращает
  2560-мерные векторы. Текущий Qdrant-индекс содержит `140` chunks;
- стоимость: индексация выполняется локально через Ollama, поэтому переменная
  cloud-стоимость равна `$0`; фактическая цена — только CPU/RAM/время локальной
  машины;
- эксплуатация: модель уже загружена в локальную Ollama, доступна через
  OpenAI-compatible `/v1/embeddings`, а сервисный sqlite-кеш делает повторную
  индексацию идемпотентной.

Размерность `2560` больше, чем у `text-embedding-3-small`, но для текущего и
ожидаемого объёма это не является узким местом. Даже `100 000` chunks в float32 дают
примерно `100000 * 2560 * 4 ≈ 1 GB` сырых векторов до overhead индекса и
metadata. Для базы знаний PR-review ассистента это приемлемый обмен: индекс
сохраняет качество retrieval на multilingual/code material без cloud-индексации.

Cloud fallback оставлен через `.env`, если локальная Ollama недоступна:

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small
```

Ключ кеша включает модель, provider, размерность и тип входа, поэтому смена
модели автоматически создаёт новые embedding-записи и не смешивает векторы
разной размерности.

Для `qwen3-embedding` запросы и документы кодируются асимметрично:
`embed_query()` добавляет instruction-префикс, а `embed_documents()` оставляет
passage без префикса. Маркеры `Instruct:` и `Query:` оставлены на английском,
потому что это служебный формат Qwen3 Embedding, а не текст для пользователя;
сама инструкция явно нацеливает модель на русские и английские вопросы и
документы. Для E5-моделей сервис использует `query:` и `passage:`.

## Контрольный mini-benchmark

Mini-benchmark лежит в `tests/eval/mini_benchmark.json`. Он устроен как набор
парных сравнений: пользовательский вопрос, релевантный фрагмент документа и
нерелевантный фрагмент из той же предметной области. Это не "погода против
возврата товара", а review-вопросы про Python style, PEP 484, Ansible
idempotence и handlers, секреты в коде, async FastAPI и lint suppressions.

Проверочный скрипт `scripts/run_embedding_benchmark.py` кодирует запрос через
`embed_query()`, оба документа через `embed_documents()` и считает cosine score
как dot product нормализованных векторов. Критерий простой: релевантный
фрагмент должен получить score выше нерелевантного.

Локальный прогон на `qwen3-embedding:4b`:

```text
accuracy=8/8 (100.0%)
mean_margin=+0.2893
min_margin=+0.1644
dimensions=2560
```

Минимальный margin положительный (`+0.1644`). На данном наборе модель отделяет
релевантные фрагменты от нерелевантных. Оценка не заменяет retrieval-eval на
полной Qdrant-коллекции; результаты эксперимента по метрике и фильтрации
зафиксированы в `docs/vector_store.md`.

## Smoke-проверка кеша

Первый прогон создаёт embedding и пишет его в sqlite-кеш:

```bash
OPENAI_API_KEY=ollama \
OPENAI_BASE_URL=http://host.docker.internal:11434/v1 \
EMBEDDING_PROVIDER=openai \
EMBEDDING_MODEL=qwen3-embedding:4b \
uv run python scripts/embedding_smoke.py "Как работает Redis cache-aside в /chat?"
```

Повторите ту же команду второй раз. При неизменных `EMBEDDING_MODEL`,
`EMBEDDING_DIMENSIONS` и тексте запрос в provider не выполняется: вектор
читается из `EMBEDDING_CACHE_PATH`, а latency должна стать заметно ниже.
В проверенном локальном прогоне одиночный smoke-вызов снизился с `1407 ms` до
`0.70 ms`, а полный mini-benchmark — с `3142 ms` до `9.76 ms`.

## Стоимость индексации

Для локальной `qwen3-embedding:4b` денежная стоимость индексации равна `$0`:
модель работает через Ollama. Фактическая стоимость — CPU/RAM/время локального
inference. Текущий индекс содержит `140` chunks из `14` документов.

Для cloud fallback OpenAI считает страницу как примерно 800 токенов. Из таблицы
OpenAI:

| Модель | Страниц на $1 | Оценка цены за 1M токенов |
| --- | ---: | ---: |
| `text-embedding-3-small` | 62 500 | ~$0.02 |
| `text-embedding-3-large` | 9 615 | ~$0.13 |

Оценка для cloud fallback:

| Сценарий | Документов | Средний объём | Всего токенов | `3-small` | `3-large` |
| --- | ---: | ---: | ---: | ---: | ---: |
| Минимальный | 50 | 1 500 токенов | 75 000 | ~$0.0015 | ~$0.0098 |
| Базовый | 50 | 2 500 токенов | 125 000 | ~$0.0025 | ~$0.0163 |
| С запасом на chunk overlap | 60 | 3 000 токенов | 180 000 | ~$0.0036 | ~$0.0234 |

Если индексация выполняется через Batch API, OpenAI даёт скидку 50% по
сравнению с синхронными endpoint'ами, поэтому базовый сценарий для
`text-embedding-3-small` будет порядка `$0.00125`.

## Ссылки

- OpenAI Embeddings Guide: https://platform.openai.com/docs/guides/embeddings
- OpenAI Batch API: https://platform.openai.com/docs/guides/batch
- Qwen3 Embedding model card: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- Ollama qwen3-embedding: https://ollama.com/library/qwen3-embedding
- Sentence Transformers API: https://sbert.net/docs/package_reference/sentence_transformer/SentenceTransformer.html
