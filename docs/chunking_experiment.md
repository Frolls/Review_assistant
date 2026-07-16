# Chunking experiment

## Цель и данные

Замер выполнен 16 июля 2026 года на `data/retrieval-corpus`: 10 документов по Python и Ansible, подготовленных по официальным PEP и Ansible Community Documentation. Golden dataset содержит 36 вручную размеченных вопросов по 10 уникальным source ID. В набор входят точечные факты, сравнения и многошаговые review-сценарии.

Корпус занимает 63 434 bytes на диске и содержит 13 592 токена `cl100k_base`. Размер документа — от 1 051 до 1 658 токенов, среднее — 1 359.2. Поэтому каждый источник разбивается на несколько chunks при размере 512.

| Документ | Bytes | Tokens |
| --- | ---: | ---: |
| `ansible_check_diff_mode.md` | 4 910 | 1 051 |
| `ansible_handlers_error_handling.md` | 6 896 | 1 438 |
| `ansible_playbook_practices.md` | 7 662 | 1 658 |
| `ansible_roles.md` | 5 946 | 1 232 |
| `ansible_vault_become.md` | 6 276 | 1 296 |
| `pep257_docstrings.md` | 5 486 | 1 189 |
| `pep484_type_hints.md` | 7 517 | 1 641 |
| `pep544_protocols.md` | 5 849 | 1 261 |
| `pep589_typed_dict.md` | 5 835 | 1 222 |
| `pep8_style_guide.md` | 7 057 | 1 604 |

Для основного эксперимента использована `qwen3-embedding:4b`, размерность 2560, distance COSINE. Query embeddings вычислялись до таймера. Retrieval latency — среднее время поиска Qdrant по 36 вопросам; для строки с re-ranker указано полное время `Qdrant + rerank`.

## Стратегии и размер индекса

- `fixed`: `TokenTextSplitter(chunk_size=512, chunk_overlap=64)`.
- `recursive`: `SentenceSplitter` с `paragraph_separator="\n\n"` и tokenizer для русских предложений, `chunk_size=512`, `chunk_overlap=64`.
- `semantic`: `SemanticSplitterNodeParser(buffer_size=1, breakpoint_percentile_threshold=95)` с той же embedding-моделью.

| Стратегия | Qdrant-коллекция | Всего chunks | Среднее chunks/документ | Средняя длина, tokens |
| --- | --- | ---: | ---: | ---: |
| fixed | `docs_fixed` | 34 | 3.40 | 444.00 |
| recursive | `docs_recursive` | 34 | 3.40 | 399.76 |
| semantic | `docs_semantic` | 31 | 3.10 | 438.35 |

После замера коллекции сохранены в persistent Qdrant. Все три используют vectors size 2560 и COSINE.

## Метрики retrieval и re-ranker

| Стратегия | Hit Rate@5 | MRR@10 | Recall@10 | Средняя длина chunk | Retrieval, ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| fixed 512/64 | 1.0000 | 0.9537 | 1.0000 | 444.00 | 2.92 |
| recursive 512/64 | 1.0000 | 0.9352 | 1.0000 | 399.76 | 2.68 |
| semantic | 1.0000 | 0.9722 | 1.0000 | 438.35 | 2.83 |
| semantic, без re-ranker | 1.0000 | 0.9722 | 1.0000 | 438.35 | 3.00 |
| semantic + `BAAI/bge-reranker-v2-m3`, top-N=10 | 1.0000 | 1.0000 | 1.0000 | 438.35 | 10 498.19 |

Semantic — лучшая стратегия при одинаковом baseline-конфиге. BGE поднимает MRR@10 до 1.0000, но средняя CPU latency 10.50 s исключает его из interactive path без GPU, batching или более лёгкой модели.

## Подбор chunk_size, overlap и top-K

Размерные параметры применимы к fixed и recursive, поэтому сетка прогнана для обеих стратегий. Semantic splitter настраивается через buffer и breakpoint threshold и в эту таблицу не входит.

| Стратегия | chunk_size | overlap | top-K | Chunks | Hit Rate@5 | MRR@10 | Recall@10 | Retrieval, ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| fixed | 256 | 32 | 10 | 66 | 1.0000 | 0.9722 | 1.0000 | 2.66 |
| fixed | 256 | 32 | 20 | 66 | 1.0000 | 0.9722 | 1.0000 | 2.89 |
| fixed | 512 | 64 | 10 | 34 | 1.0000 | 0.9537 | 1.0000 | 2.74 |
| fixed | 512 | 64 | 20 | 34 | 1.0000 | 0.9537 | 1.0000 | 3.14 |
| recursive | 256 | 32 | 10 | 67 | 1.0000 | 0.9861 | 1.0000 | 2.64 |
| recursive | 256 | 32 | 20 | 67 | 1.0000 | 0.9861 | 1.0000 | 2.84 |
| recursive | 512 | 64 | 10 | 34 | 1.0000 | 0.9352 | 1.0000 | 2.70 |
| recursive | 512 | 64 | 20 | 34 | 1.0000 | 0.9352 | 1.0000 | 3.08 |

Recursive 256/32 дал максимальный MRR@10 без re-ranker: 0.9861. `top-K=20` не улучшил ни одну метрику и увеличил latency. Итоговые defaults в `app/core/config.py`: `RAG_CHUNK_SIZE=256`, `RAG_CHUNK_OVERLAP=32`, `RAG_SIMILARITY_TOP_K=10`.

## Сравнение Qwen3 Embedding 4B и 0.6B

Обе модели прогнаны на одинаковых 10 документах, 36 вопросах и baseline-конфиге 512/64. Для 0.6B использован отдельный embedded Qdrant с vector size 1024; persistent коллекции 4B не изменялись.

| Модель | Стратегия | Hit Rate@5 | MRR@10 | Recall@10 |
| --- | --- | ---: | ---: | ---: |
| `qwen3-embedding:4b` | fixed | 1.0000 | 0.9537 | 1.0000 |
| `qwen3-embedding:4b` | recursive | 1.0000 | 0.9352 | 1.0000 |
| `qwen3-embedding:4b` | semantic | 1.0000 | 0.9722 | 1.0000 |
| `qwen3-embedding:0.6b` | fixed | 1.0000 | 0.9676 | 1.0000 |
| `qwen3-embedding:0.6b` | recursive | 1.0000 | 0.9167 | 1.0000 |
| `qwen3-embedding:0.6b` | semantic | 0.9722 | 0.9352 | 1.0000 |

| Модель | GGUF | Vector dim | Embed mean, ms | Median, ms | P95, ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| `qwen3-embedding:4b` | 2.5 GB | 2560 | 110.88 | 105.98 | 138.72 |
| `qwen3-embedding:0.6b` | 639 MB | 1024 | 92.47 | 92.40 | 97.41 |

0.6B уменьшает файл модели примерно в 3.9 раза и raw vector в 2.5 раза. Средний query embedding быстрее на 18.41 ms, но semantic теряет один Hit@5 и 0.0370 MRR. 4B остаётся default: она даёт лучший результат по одинаковой semantic-стратегии и максимальный результат после tuning recursive 256/32.

## Вывод

Выбираю стратегию **recursive с конфигом `(chunk_size=256, overlap=32, top-K=10)`**, потому что она дала Hit Rate@5=1.0000, MRR@10=0.9861 и Recall@10=1.0000 при 2.64 ms. Это выше semantic на 0.0139 MRR и fixed 256/32 на 0.0139 MRR. `top-K=20` не дал прироста. На semantic baseline BGE re-ranker доводит MRR до 1.0000, но CPU latency 10.50 s оставляет его только в offline quality mode. Основной embedding остаётся `qwen3-embedding:4b`.

## Воспроизведение

```bash
# Optional dependency для BGE re-ranker
uv sync --extra local-embeddings

# Используются data/retrieval-corpus и tests/eval/retrieval_dataset.json
python scripts/run_chunking_experiment.py

# Uncached query-embedding latency после warm-up
python scripts/compare_embedding_latency.py
```

Скрипт пересоздаёт `docs_fixed`, `docs_recursive` и `docs_semantic`, выводит corpus/chunk statistics и retrieval metrics, затем выполняет восемь tuning-прогонов: четыре fixed и четыре recursive.
