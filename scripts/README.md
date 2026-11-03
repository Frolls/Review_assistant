# Проверки и измерения

Команды ниже запускаются из корня backend-репозитория. Полный стенд и настройка
окружения описаны в [DEPLOYMENT.md](../DEPLOYMENT.md).

| Скрипт | Назначение |
| --- | --- |
| `eval_code_review.py` | Отправляет реальные вложения в `/chats`, сохраняет SSE, историю, usage и отдельные оценки TP/FP/FN |
| `measure_local.py` | Измеряет время команды, CPU/RAM/GPU и загруженные модели на хосте inference |
| `benchmark_inference.py` | Сохраняет warmup и native-счётчики Ollama для каждого повтора |
| `smoke_pair.py` | Readiness backend и бота, RAG с источником, preview/cancel без подтверждения отправки |
| `test_pair.sh` | Проверяет настоящий клиент бота и API с детерминированной моделью, без сети |
| `test_all.sh` | Собирает тестовые образы и выполняет backend, bot и pair tests |
| `recompute_verification.py` | Проверяет хеши архивов и пересчитывает итоговые метрики без модели или сервисов |

Для pair test нужен соседний репозиторий бота; другой путь задаётся через
`BOT_REPO=/path/to/telegram-review-bot sh scripts/test_pair.sh`. Образ
`localhost/llm-service:v1` должен быть собран по инструкции развёртывания.

Проверка сохранённых доказательств без соседнего репозитория:

```bash
python3 scripts/recompute_verification.py --verify-only
```

Проверяются текущие и [исторические наборы](../docs/verification/HISTORY.md),
включая манифесты `artifact-sha256.json` и `artifacts-sha256.json`.
Полный пересчёт ниже относится только к итоговому v12.

Полный пересчёт в исходной раскладке временного каталога:

```bash
python3 scripts/recompute_verification.py --bot-repo ../telegram-review-bot
```

Runner проверяет точное совпадение исходников с записанными хешами, включая
отдельную правку форматирования после live-прогона. При последующих изменениях
кода используйте соответствующий коммит; ошибка хеша не означает пересчёт на
новой версии. Исходные ответы и числовые измерения не изменяются. Редакционные
правки атрибуции и отдельное изменение настройки JSON I/O зафиксированы в
[журнале сопровождения](../docs/verification/maintenance-2026-11-03.json).
Runner принимает только указанные там хеши после изменения; исходный live-протокол
не переписывается. Его временная копия использует эти хеши при пересчёте метрик.

При переносе у `review_eval.py` удалена одна лишняя пустая строка в конце файла.
Для воспроизведения сохранён исходный snapshot; runner принимает только это
точное изменение EOF и проверяет hash snapshot. Изменение содержимого функции
по-прежнему приводит к ошибке проверки.

Новый benchmark при работающем стенде и `INTERNAL_TOKEN` в окружении:

```bash
mkdir -p var/verification
python scripts/eval_code_review.py \
  --cases tests/eval/code_review/holdout-v12-2026-11-03.json \
  --output var/verification/review-raw.json
python scripts/measure_local.py \
  --output var/verification/inference-resources.json \
  --ollama-url http://localhost:11434 --baseline-seconds 5 -- \
  python scripts/benchmark_inference.py --model qwen2.5:14b \
  --context 8192 --thinking off --repetitions 3 \
  --output var/verification/inference-raw.json
```

Новые ответы размечаются отдельно; `--score` не принимает незаполненные оценки.
Не записывайте новые результаты поверх датированных архивов. Ресурсный измеритель
запускайте на хосте модели: его показатели включают другие процессы.

Живые девять ходов через `BackendClient` и запуск HTTP API бота без polling
находятся в [репозитории бота](https://github.com/Frolls/telegram-review-bot/blob/main/scripts/README.md).
Синтетические и реальные проверки различаются: pair test не доказывает качество
модели или доставку в Telegram. Полные результаты и ограничения —
в [актуальном отчёте](../docs/verification/README.md).
