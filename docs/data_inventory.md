# Инвентаризация RAG-корпуса

Снимок: 27 июля 2026 года, после запуска `python scripts/download_data.py`.

## Итог

| Набор | Категория | Формат | Файлов | Размер |
| --- | --- | ---: | ---: | ---: |
| Официальные Python Enhancement Proposals | `python-peps` | HTML | 36 | 3 259 201 байт |
| Чеклисты code review | `rag-block-03` | Markdown | 10 | 18 223 байта |
| Python/Ansible retrieval corpus | `retrieval-corpus` | Markdown | 10 | 20 695 байт |
| **Индексируемый корпус** | 3 категории | **HTML + Markdown** | **56** | **3 298 119 байт (3.15 MiB)** |
| Legacy knowledge export, ingestion не поддерживает JSON | корень `data` | JSON | 1 | 31 508 байт |
| **Весь каталог `data/`** |  |  | **57** | **3 329 627 байт (3.18 MiB)** |

Таким образом, `python scripts/ingest.py data/` видит 56 поддерживаемых файлов
двух форматов. Reader-слой также поддерживает PDF и DOCX; новые файлы этих
форматов можно положить в любую категорию или загрузить через
`POST /documents/upload`.

Reader-тест создаёт и извлекает текст из всех четырёх форматов, включая author
из DOCX core properties и page из PDF. В end-to-end прогоне отдельный PDF был
загружен через API и добавил одну точку в runtime-коллекцию. Runtime upload и
намеренно повреждённые `.failed`-файлы хранятся в Docker volume и не входят в
этот репозиторный снимок.

## Происхождение

36 HTML-документов скачаны с `https://peps.python.org/pep-NNNN/` скриптом
`scripts/download_data.py`. Список номеров зафиксирован в коде, downloader не
перезаписывает уже скачанные файлы без `--force` и проверяет минимальный размер
и HTML-сигнатуру ответа.

Markdown-корпуса относятся к предметной области диплома: Python style/typing/
testing, secure review, API и database migrations, architecture, observability,
Ansible playbooks/roles/handlers/vault и контроль fallback.

## Воспроизводимая проверка

```bash
find data -type f \( -name '*.md' -o -name '*.html' -o -name '*.pdf' -o -name '*.docx' \) \
  -printf '%s %p\n'
python scripts/download_data.py
python scripts/ingest.py data/
```

Значения размера могут измениться, если peps.python.org обновит HTML уже
существующего PEP и downloader запущен с `--force`; после такого обновления
инвентаризацию нужно пересчитать.
