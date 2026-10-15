# Запуск полного стенда

Поместите общий `compose.yaml` и `.env.example` в рабочую папку.
Из неё клонируйте оба репозитория и создайте `.env`:

```bash
git clone https://github.com/Frolls/Review_assistant.git Review_bot
git clone https://github.com/Frolls/telegram-review-bot.git telegram-review-bot
# При повторной настройке сохраните существующий .env.
if [ ! -e .env ]; then cp .env.example .env; fi
```

Получится следующая структура:

```text
Diploma/
├── compose.yaml
├── .env.example
├── .env                   # локальные настройки и ключи
├── Review_bot/            # backend
└── telegram-review-bot/
```

Нужны Docker с Compose и Ollama на хосте, доступная из контейнеров по
`http://host.docker.internal:11434`. Ollama запускается отдельно от Compose;
она должна принимать подключения с контейнерной сети, а не только с loopback.
Заранее загрузите модели:

```bash
ollama pull qwen3:8b
ollama pull qwen3-embedding:4b
```

В **общем** `.env` укажите действующий `BOT_TOKEN`, свои `INTERNAL_TOKEN` и
`ADMIN_TOKEN`. Общий Compose передаст одинаковые ключи обоим сервисам.
Основные настройки модели:

```dotenv
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
DEFAULT_MODEL=qwen3:8b
EMBEDDING_MODEL=qwen3-embedding:4b
EMBEDDING_DIM=2560
RAG_SCORE_THRESHOLD=0.5
```

Из общей папки `Diploma/`:

```bash
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml ps
# После завершения индексации и запуска API:
curl --fail http://localhost:8000/ready
```

Запускаются API, бот, PostgreSQL, Redis, Qdrant и Phoenix. До запуска API
выполняются индексация корпуса и миграции БД; первая индексация может занять
несколько минут. Бот начинает работу после готовности API.

- [Swagger](http://localhost:8000/docs) — интерактивный API.
- [Readiness](http://localhost:8000/ready) — Redis, PostgreSQL, Qdrant, API модели, RAG и агент.
- [Phoenix](http://localhost:6006) — трассы запросов.
- В Telegram отправьте своему боту `/start`, затем вопрос или файл кода.

Для диагностики из общей папки:

```bash
docker compose -f compose.yaml logs --tail=100 app bot
```

Общий Compose публикует порты только на loopback хоста и сохраняет данные в
volumes, включая журнал доставки сообщений бота. `down` останавливает стек;
добавление `-v` удаляет его постоянные данные.

Используйте `compose.yaml` из общей папки. Файл `Review_bot/compose.yaml`
предназначен для отдельной конфигурации разработки.

## Проверки

Тесты обоих сервисов из общей папки:

```bash
docker compose -f compose.yaml --profile test run --rm --build tests
docker compose -f compose.yaml --profile test run --rm --build bot-tests
```

Для тестов backend используется отдельная временная PostgreSQL.
