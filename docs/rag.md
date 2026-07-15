# RAG Block 03

## Зависимости

Проектный `pyproject.toml` фиксирует минимальные версии:

- `llama-index>=0.12.0`
- `llama-index-vector-stores-qdrant>=0.4.0`
- `llama-index-readers-file>=0.4.0`
- `qdrant-client>=1.14.0,<1.16`
- `openai>=2.38.0,<3`

Метапакет `llama-index` подтягивает core, OpenAI LLM и OpenAI embeddings. Отдельно добавлены Qdrant vector store и файловые reader'ы, потому что они не являются частью минимального ядра.

В dev-образе, пересобранном для проверки 2026-07-15, установлены: `llama-index==0.14.23`, `llama-index-core==0.14.23`, `llama-index-vector-stores-qdrant==0.8.8`, `llama-index-readers-file==0.6.0`, `qdrant-client==1.15.1`, `openai==2.38.0`.

## Корпус

Учебный корпус лежит в `data/rag-block-03` и содержит 10 Markdown-файлов по предметной области диплома: правила и чеклисты для ИИ-ассистента, который ревьюит код. В корпусе 9 доменных документов и 1 контрольный нерелевантный документ для fallback-проверки.

- `python_style_review.md`
- `python_typing_review.md`
- `python_tests_review.md`
- `secure_code_review.md`
- `api_contract_review.md`
- `database_migration_review.md`
- `ansible_best_practices.md`
- `architecture_review.md`
- `observability_review.md`
- `unrelated_fallback_control.md`

Файл `unrelated_fallback_control.md` заведомо вне предметной области code review и нужен только для проверки fallback-сценария. Остальные файлы описывают именно знания, которые ассистент использует при ревью PR: стиль Python, typing, тесты, security, API-контракты, миграции БД, Ansible, архитектуру и observability.

## Коллекции

Для блока используется отдельная LlamaIndex-коллекция `rag_block_03_diploma`. Старый индекс `documents` наполнялся напрямую через `qdrant-client` с плоским payload, а LlamaIndex хранит ноды в своём формате, включая `_node_content`. Если подключаться к чужой коллекции через `from_vector_store`, `source_nodes` и metadata будут неполными.

Bare-metal реализация использует отдельную коллекцию `rag_block_03_diploma_baremetal`, чтобы сравнение не смешивало разные payload-схемы.

Параметры индекса:

- vector size: `EMBEDDING_DIM=2560`
- distance: `COSINE`
- embed model: `EMBEDDING_MODEL=qwen3-embedding:4b`
- chunk size: `RAG_CHUNK_SIZE=512`
- chunk overlap: `RAG_CHUNK_OVERLAP=64`
- top k: `RAG_SIMILARITY_TOP_K=3`

Размерность, distance и embed-модель должны совпадать с тем, что лежит в коллекции. Несовпадение размерности считается ошибкой запуска.

## FastAPI

LlamaIndex query engine использует явный QA prompt: модель должна отвечать только по найденному контексту, не использовать внешние знания и честно сообщать, если в корпусе нет ответа. Дополнительно ответ отсекается по `RAG_MIN_TOP_SCORE`.

Маршрут `POST /rag/query` принимает:

```json
{"question": "Почему в Ansible task лучше избегать command и shell?"}
```

Ответ:

```json
{
  "answer": "...",
  "top_score": 0.721,
  "sources": [
    {"text": "...", "source": "ansible_best_practices.md", "score": 0.721}
  ]
}
```

Индекс создаётся один раз в `lifespan` приложения и сохраняется в `app.state.rag_service`; endpoint не пересоздаёт индекс на каждый запрос. Если RAG-инфраструктура недоступна на старте, основной FastAPI-сервис продолжает запускаться, а `POST /rag/query` возвращает `503` с кодом `rag_unavailable`.

Живая HTTP-проверка выполнена 2026-07-15:

```bash
docker compose run --rm --no-deps -p 8001:8000 \
  -e OPENAI_BASE_URL=http://host.docker.internal:11434/v1 \
  -e QDRANT_URL=http://host.docker.internal:6333 \
  -e CHAT_REPOSITORY=json \
  app uvicorn app.main:app --host 0.0.0.0 --port 8000

curl -sS -X POST http://localhost:8001/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"Что делать, если секрет уже попал в diff?"}'
```

Ответ вернул `answer`, `top_score=0.554` и 3 источника; top-1 source — `secure_code_review.md`.

## LlamaIndex vs Bare-metal

| Критерий | LlamaIndex | Bare-metal |
| -------- | ---------- | ---------- |
| Строк кода (ingestion + query, без импортов) | примерно 115 | примерно 155 |
| Поддержка форматов из коробки | `SimpleDirectoryReader` плюс `llama-index-readers-file` умеют читать `.md`, `.txt`, `.pdf`, `.docx`, `.html` и другие форматы через reader'ы | Нужно вручную поддерживать каждый формат; сейчас добавлены `.md`, `.txt`, `.html`, `.pdf`, `.docx` |
| Что дописать для PDF/DOCX | Подключить reader-пакет и положить файлы в корпус | Вызвать `pypdf`/`python-docx`, ограничить страницы и обработать сканы/битые файлы |
| Что дописать для batch-ingestion / async | Настроить ingestion pipeline, batch size и async reader/vector-store операции | Самостоятельно писать batch embedding, retry, backpressure, async Qdrant/OpenAI клиенты |
| Где удобнее дебажить top_score / source_nodes | Удобно смотреть `response.source_nodes`, но часть payload спрятана в формате LlamaIndex | Удобнее видеть каждый payload и score, потому что весь retrieval-контракт написан явно |
| Где гибче подменять компоненты (re-ranker, chunker) | Быстрее заменить готовый компонент через Settings/pipeline | Максимальная гибкость, но каждый компонент и его контракт нужно поддерживать руками |

В дипломной версии логичнее оставить LlamaIndex как основной путь: он короче, лучше покрывает файловый ingestion и быстрее показывает полный RAG-cycle. Bare-metal версия остаётся рядом как учебное сравнение и как способ объяснить, что именно скрывают `SimpleDirectoryReader`, `SentenceSplitter`, `VectorStoreIndex` и `QueryEngine`. Для production-доработки всё равно понадобятся явные фильтры tenant/access, eval и observability вокруг retrieval.

## Прогон 5 Вопросов

Живой прогон выполнен 2026-07-15 через LlamaIndex-реализацию командой:

```bash
docker compose run --rm --no-deps \
  -e OPENAI_BASE_URL=http://host.docker.internal:11434/v1 \
  -e QDRANT_URL=http://host.docker.internal:6333 \
  -v ./scripts:/app/scripts:ro \
  app python scripts/verify_rag_block03.py
```

Использовались Ollama `qwen3`, embedding-модель `qwen3-embedding:4b`, Qdrant-коллекция `rag_block_03_diploma`, `RAG_SIMILARITY_TOP_K=3`, `RAG_MIN_TOP_SCORE=0.2`.

### 3 хороших

1. Вопрос: Почему в Ansible task лучше избегать command и shell?
   Краткий ответ: `command` и `shell` хуже сохраняют идемпотентность, требуют явной проверки состояния, `creates`/`removes` или корректного `changed_when`; вместо них лучше использовать специализированные модули и handler для рестартов.
   Top-1 source: `ansible_best_practices.md`, score `0.657`.
   Оценка: релевантно.
   Гипотеза: прямое совпадение по `Ansible`, `command`, `shell` и `идемпотентность`; retrieval попал в нужный документ.

2. Вопрос: Что делать, если секрет уже попал в diff?
   Краткий ответ: запросить ротацию секрета, убедиться, что значение удалено из истории, и обработать ситуацию по внутреннему incident-процессу.
   Top-1 source: `secure_code_review.md`, score `0.553`.
   Оценка: релевантно.
   Гипотеза: термин `секрет` и фраза `попал в diff` напрямую совпали с security-чеклистом.

3. Вопрос: Как безопасно добавить NOT NULL колонку в большую таблицу?
   Краткий ответ: сначала добавить nullable-колонку, заполнить её батчами, затем отдельным коротким шагом применить `NOT NULL`; нельзя делать один большой `UPDATE`, который блокирует таблицу.
   Top-1 source: `database_migration_review.md`, score `0.641`.
   Оценка: релевантно.
   Гипотеза: `NOT NULL`, `большая таблица` и сценарий backfill хорошо совпали с документом про миграции.

### 1 средний

4. Вопрос: Как отревьюить PR, где endpoint пишет в базу, вызывает внешний HTTP API и форматирует ответ в одной функции?
   Краткий ответ: не принимать смешивание HTTP-валидации, бизнес-логики, БД, внешнего HTTP и форматирования ответа в одной функции; разделить код на endpoint, service, repository/client и проверить контракт ошибок.
   Top-1 source: `api_contract_review.md`, score `0.660`; top-2 `architecture_review.md`, score `0.597`.
   Оценка: релевантно.
   Гипотеза: вопрос широкого типа; retrieval первым поднял API-контракт из-за слов `endpoint`, `форматирует ответ`, `HTTP API`, но второй источник дал архитектурные границы ответственности.

### 1 вне базы

5. Вопрос: Когда лучше высаживать томаты в открытый грунт?
   Краткий ответ: `В корпусе RAG не нашлось достаточно релевантной информации для ответа.`
   Top-1 source: `unrelated_fallback_control.md`, score `0.158`.
   Оценка: fallback сработал корректно.
   Гипотеза: вопрос намеренно вне базы review assistant; top score ниже `RAG_MIN_TOP_SCORE=0.2`, поэтому генерация была отсечена.

Итог: фактический прогон подтвердил схему `3 хороших / 1 средний / 1 вне базы`. Для предметных вопросов top-1 или top-2 source соответствует ожидаемому документу, а внебазовый вопрос не привёл к выдуманному ответу.
