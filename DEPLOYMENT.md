# Запуск полного стенда

Общий Compose и шаблон настроек теперь хранятся в этом репозитории:
[deploy/compose.yaml](deploy/compose.yaml), [deploy/.env.example](deploy/.env.example).
Файлы из невключённой в Git папки Diploma больше не требуются.

Клонируйте backend и бот в одну рабочую папку с указанными именами:

```bash
git clone https://github.com/Frolls/Review_assistant.git Review_bot
git clone https://github.com/Frolls/telegram-review-bot.git telegram-review-bot
if [ ! -e .env ]; then cp Review_bot/deploy/.env.example .env; fi
```

Все Compose-команды ниже выполняются из этой общей папки. `--project-directory`
задаёт базу относительных путей; менять файлы или копировать Compose в корень
не нужно. Если `.env` уже существует, сохраните ключи и сравните новые параметры
с шаблоном вручную.

Нужны Docker с Compose и Ollama на хосте, доступная из контейнеров по
`http://host.docker.internal:11434`. Ollama запускается отдельно; её адрес должен
принимать подключения из контейнерной сети. Для проверенной конфигурации:

```bash
ollama pull qwen3:8b
ollama pull qwen2.5:14b
ollama pull qwen3-embedding:4b
```

В локальном `.env` задайте свой `BOT_TOKEN`, общие `INTERNAL_TOKEN` и
`ADMIN_TOKEN`. Ключи не должны попадать в Git. Основные настройки:

```dotenv
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
DEFAULT_MODEL=qwen3:8b
REVIEW_MODEL=qwen2.5:14b
EMBEDDING_MODEL=qwen3-embedding:4b
EMBEDDING_DIM=2560
LLM_NUM_CTX=8192
RAG_SCORE_THRESHOLD=0.5
OBSERVABILITY_INCLUDE_CONTENT=false
```

Для Ollama приложение создаёт и проверяет производные aliases с нужным
`num_ctx`; исходные модели не изменяются. Пустой `REVIEW_MODEL` использует
основную модель, но это будет другая конфигурация, чем в зафиксированном benchmark.
Имена существующих коллекций и volumes не меняйте при обновлении: новый шаблон
не означает необходимости удалить или переиндексировать уже рабочую базу.

```bash
docker compose --project-directory "$PWD" --env-file .env \
  -f Review_bot/deploy/compose.yaml config --quiet
docker compose --project-directory "$PWD" --env-file .env \
  -f Review_bot/deploy/compose.yaml up -d --build
curl --fail http://localhost:8000/ready
```

Запускаются backend, Telegram-бот, PostgreSQL, Redis, Qdrant и Phoenix. До API
выполняются ingestion и миграции; первая индексация может занять несколько минут.
Бот запускает polling после готовности backend. Не запускайте второй экземпляр
polling с тем же токеном. API доступны на loopback: backend `8000`, Phoenix `6006`.
Bot backchannel доступен внутри Compose по `http://bot:8081`.

История и checkpoints сохраняются в PostgreSQL, журнал доставки — в volume бота,
заявки эскалации — в `RAG_PIPELINE_STORAGE_DIR` на постоянном volume backend.
`/clear` очищает чат и агента, но не журнал уже выполненных доставок.

```bash
docker compose --project-directory "$PWD" --env-file .env \
  -f Review_bot/deploy/compose.yaml logs --tail=100 app bot
```

`down` останавливает стек; добавление `-v` удаляет постоянные данные.
`compose.yaml` непосредственно в backend-репозитории остаётся отдельным
вариантом разработки, а `deploy/compose.yaml` — общим стендом двух репозиториев.

## Проверки

Из общей папки:

```bash
sh Review_bot/scripts/test_all.sh
```

Используется отдельная тестовая PostgreSQL без рабочего volume. Скрипт собирает
образы и проверяет Ruff, backend, бот и пару сервисов. Самостоятельные команды
и live harness описаны в [scripts/README.md](scripts/README.md).

Актуальные [результаты и ограничения](docs/verification/README.md): 257 тестов
backend, 39 тестов бота и один pair smoke; живой benchmark и inference измерены
отдельно. Зафиксированный live-прогон использовал read-only mounts исходников,
а не новый build образов. Проверка Compose-конфигурации и перенос документации
не считаются новым развёртыванием или повторным live benchmark.
