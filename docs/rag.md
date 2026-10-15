# Корпоративный RAG

## Архитектура

```mermaid
flowchart LR
    subgraph I["Ingestion contour"]
        Files["data/category/*<br/>PDF · DOCX · HTML · MD"]
        Readers["PyMuPDFReader<br/>DocxReader<br/>HTMLTagReader<br/>MarkdownReader"]
        Meta["Metadata enrichment<br/>source · dates · author<br/>category · version · page"]
        Split["SentenceSplitter<br/>256 / overlap 32"]
        Embed["OpenAI-compatible embeddings<br/>qwen3-embedding:4b"]
        Docstore["SimpleDocumentStore<br/>persistent hashes"]
        Qdrant[("Qdrant<br/>corporate_rag")]
        Files --> Readers --> Meta --> Split --> Embed --> Qdrant
        Meta --> Docstore
        Docstore -->|"DocstoreStrategy.UPSERTS"| Split
    end

    subgraph Q["Query contour"]
        Client["HTTP / Telegram"]
        History[("Postgres chat history")]
        Condense["Optional condense<br/>follow-up → standalone query"]
        Retrieve["Vector retrieval<br/>top_k = 10"]
        Guard{"Есть фрагменты со score ≥ 0.5?"}
        Rerank["Optional BGE rerank<br/>top_n = 5"]
        Evidence{"Проверка вопроса и достаточности фактов"}
        Clarify["Уточнение или отказ без источников"]
        Prompt["Numbered context<br/>[1], [2], …"]
        LLM["OpenAI-compatible LLM<br/>генерация и проверка ответа"]
        Refusal["Fixed refusal<br/>no answer LLM call"]
        SSE["SSE tokens + final<br/>sources, confident"]
        Client --> Condense
        History --> Condense
        Condense --> Retrieve --> Guard
        Guard -->|yes| Rerank --> Evidence
        Evidence -->|достаточно| Prompt --> LLM --> SSE
        Evidence -->|неясно или недостаточно| Clarify --> SSE
        History --> Prompt
        Guard -->|no| Refusal --> SSE
    end

    Qdrant --> Retrieve
```

Ingestion и query разделены. FastAPI не переиндексирует корпус во время обычного
запроса. Compose выполняет `scripts/ingest.py data/` перед стартом приложения,
а `POST /documents/upload` запускает тот же pipeline как background task.

## Ingestion

`app/services/ingestion.py` явно выбирает reader по расширению:

- PDF — `PyMuPDFReader` (`pymupdf>=1.24`);
- DOCX — `DocxReader`; author читается из DOCX core properties через
  `python-docx`;
- HTML/Confluence export — `HTMLTagReader(tag="body")`;
- Markdown — `MarkdownReader`.

Для каждого документа добавляются `source`, `file_name`, `file_path`,
`last_modified`, `created_at`, `author` при наличии, первый сегмент
`data/<category>/...` как `category`, версия из имени вида `*-v1.2.*` и `page`.
Технические значения перечислены в `excluded_embed_metadata_keys`, поэтому пути,
даты, номера страниц и служебные HTML-поля не меняют смысл embedding.

Pipeline:

```text
readers → metadata → SentenceSplitter(256, 32) → embedding → Qdrant
                         ↕
              SimpleDocumentStore + UPSERTS
```

`SimpleDocumentStore` сохраняется в `RAG_PIPELINE_STORAGE_DIR`
(`var/ingestion/docstore.json`). Стабильный document id и hash позволяют
`DocstoreStrategy.UPSERTS` удалить старые чанки изменённого документа и не
добавлять ничего для неизменённого. CLI печатает итог
`0 changed, N unchanged`; повреждённый файл получает суффикс `.failed`, а ошибка
остаётся в логе.

Локальный запуск:

```bash
python scripts/download_data.py
python scripts/ingest.py data/ --show-progress
python scripts/ingest.py data/
```

## Retrieval, guard и цитаты

Последовательность в `app/services/rag.py`:

1. При наличии истории и `RAG_CONDENSE_ENABLED=true` короткий follow-up
   переписывается в самостоятельный запрос. В prompt передаётся `chat_id`, но
   отдельное хранилище не создаётся.
2. Retriever получает `RAG_SIMILARITY_TOP_K=10`.
3. Порог `RAG_SCORE_THRESHOLD` применяется к каждому кандидату до reranking.
   Если ни один не прошёл, возвращается отказ без вызова генерации ответа.
4. При `RAG_RERANKER_ENABLED=true` прошедшие порог кандидаты сортирует
   `BAAI/bge-reranker-v2-m3`; в контекст попадает до пяти фрагментов.
   Это optional dependency из extra `local-embeddings`. В стенде reranker
   выключен, используется порядок retrieval.
5. Отдельный вызов модели проверяет понятность исходного вопроса и достаточность
   найденных фактов. Возможны ответ, уточнение или отказ. Проверка увеличивает
   задержку и может ошибаться; уточнения и отказы не показывают источники.
6. Фрагменты нумеруются, системный prompt требует ссылок `[1]`, `[2]`.
   Номера JSON sources соответствуют этому контексту. Неизвестная ссылка приводит
   к отказу; отсутствующая ссылка не дописывается автоматически.

`top_score` — показатель сходства найденного контекста, а не вероятность
правильности ответа. Высокий score сам по себе не означает `confident=true`.

История Postgres передаётся генерации целиком после существующего token-budget
window. Поэтому «А для них?» понятен генератору; condense нужен только для
качества поиска.

Предметная smoke-пара для `scripts/verify_multiturn.py`:
«Почему в Ansible лучше избегать command и shell?» →
«А как для них обеспечить идемпотентность?». Вторая реплика должна быть
переписана в самостоятельный Ansible-запрос и вернуть источники из
`ansible_best_practices.md`/`ansible_playbook_practices.md`.

## Порог отказа

Кодовый default — `RAG_SCORE_THRESHOLD=0.3`. Это стартовое значение для cosine
на OpenAI `text-embedding-3-small`, указанное в задании. Локальный Compose
использует `qwen3-embedding:4b` и задаёт порог `0.5`. Такой же порог
используется в общем Compose из комплекта защиты.

Фактический прогон 27 июля 2026 года на полном 56-файловом корпусе:

| Метка | min | median | max |
| --- | ---: | ---: | ---: |
| 5 предметных запросов | 0.644 | 0.702 | 0.770 |
| 5 запросов вне базы | 0.207 | 0.305 | 0.367 |

При `0.3` три negative-запроса проходили guard, поэтому этот порог для Qwen3 4B
не принят. `0.5` лежит между наблюдаемым negative max `0.367` и positive min
`0.644`: запас до каждого класса больше 0.13. Полные вопросы, score и исходное
решение guard лежат в
[`docs/rag_score_distribution.json`](rag_score_distribution.json);
воспроизводящий скрипт — `scripts/calibrate_rag_threshold.py`. При смене корпуса
или embedding-модели распределение нужно пересчитать.

Score-guard дублируется правилом в системном prompt. Кодовая защита экономит
answer-вызов LLM, а prompt защищает от ответа по внешним знаниям при ложноположительном
retrieval.

## API

| Endpoint | Назначение |
| --- | --- |
| `POST /rag/query` | Синхронный одношаговый RAG-ответ |
| `POST /chats` | Создать или получить stateful chat |
| `POST /chats/{id}/messages` | Диалоговый SSE-поток |
| `GET /chats/{id}/messages` | История из JSON/Postgres repository |
| `POST /chats/{id}/messages/{mid}/feedback` | `up/down` и показанные sources |
| `POST /documents/upload` | Сохранить документ и поставить ingestion в background, HTTP 202 |

Пользовательские endpoint требуют `X-Internal-Token` и `X-User-ID`.
Для `/documents/upload` дополнительно нужен `X-Admin-Token`: это загрузка
в **общую** базу, отдельно от вложений личного чата. Лимит — 50 МиБ;
`202` подтверждает принятие файла, результат индексации проверяется по логам.
Примеры авторизованных запросов — в [README](../README.md) и [документации чата](chat.md).

Тело `POST /rag/query`:

```json
{
  "question": "Почему shell нежелателен в Ansible task?"
}
```

```json
{
  "answer": "Специализированный модуль проще сделать идемпотентным [1].",
  "top_score": 0.737,
  "confident": true,
  "sources": [
    {
      "id": 1,
      "file_name": "ansible_best_practices.md",
      "page": 1,
      "score": 0.737,
      "snippet": "..."
    }
  ]
}
```

SSE каждое событие сериализует через JSON; переводы строк внутри delta остаются
экранированными и не разрывают `data:` frame. Финальное событие:

```text
data: {"type":"done","message_id":"...","confident":true,"sources":[...]}
```

В диалоговом API ответ передаётся клиенту после модерации и сохранения.
Telegram-клиент разбивает длинный ответ на страницы, показывает источники
и кнопки 👍/👎; в feedback передаёт список фактически показанных источников.

## Текущая оценка

15 октября 2026 выполнен полный прогон на 35 вопросах при пороге 0.5:
faithfulness 0.781, answer relevancy 0.780, context precision 0.939,
context recall 0.986. Получены 33 ответа и два избыточных уточнения,
включённые в средние. Наличие ссылки — 0.943, критерий `> 0.95` не пройден.
Коллекция `corporate_rag_rules_v3` содержала 1818 фрагментов.
[Условия, ограничения и сырые данные](rag_evaluation_current.md).

## Историческая приёмка 27 июля 2026

Полный локальный прогон выполнен 27 июля 2026 года с Ollama
`qwen3:8b`, `qwen3-embedding:4b`, Qdrant 1.14, PostgreSQL 16 и Redis 7.4:

| Проверка | Фактический результат |
| --- | --- |
| Cold ingestion | `56 changed, 0 unchanged, 0 failed`, 1726 чанков |
| Повторный ingestion | `0 changed, 56 unchanged, 0 failed`, 0 новых чанков |
| Профильный `/rag/query` | top score `0.7598`, `confident=true`, пять sources и цитаты |
| Внебазовый запрос | score `0.2963 < 0.5`, фиксированный отказ и событие `rag.score_guard_refusal` |
| Multi-turn follow-up | condense сохранил Ansible-контекст, top score `0.7631` |
| PDF upload | HTTP 202 за 4 мс, один чанк доступен в поиске примерно через 8 секунд |
| Ошибка reader | повреждённые и повторно загруженные PDF получили уникальные имена `*.failed` |
| Feedback | `up/down` и показанные sources записаны в PostgreSQL |
| Telegram | реальный Bot API polling, backend SSE и финальное редактирование сообщения |
| Compose | обычный `docker compose up -d` поднял app, bot, Qdrant, PostgreSQL и Redis |

После тестового PDF рабочая коллекция содержала 1727 точек. Этот upload хранится
в runtime volume и не входит в репозиторный inventory из 56 файлов.

На этапе RAG-приёмки автоматические проверки дали: backend —
`93 passed, 6 skipped`; Telegram — `14 passed`. После добавления persistent
agent актуальная общая backend suite выросла до `127 passed, 6 skipped`.
Отдельный Telegram-тест подтверждает схлопывание быстрых чанков при
`MIN_EDIT_INTERVAL=0.7`.

## Запуск

Полный стенд запускается из общей папки с двумя репозиториями и приложенным
`compose.yaml`. Подготовка Ollama, ключей и структура файлов описаны
в [инструкции развёртывания](../DEPLOYMENT.md).

После подготовки из общей папки:

```bash
docker compose -f compose.yaml up -d --build
curl --fail http://localhost:8000/ready
```

Compose поднимает API, бот, Qdrant, Redis, PostgreSQL и Phoenix. Индексация
и миграции выполняются перед стартом API. Первая индексация занимает несколько
минут; healthcheck учитывает её через `start_period=5m`.
