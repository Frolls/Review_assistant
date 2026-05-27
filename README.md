# PR-review ассистент

ИИ-ассистент для ревью кода с полным циклом `tool_call` на OpenAI SDK. Он может работать либо с облачным OpenAI API, либо с локальным Ollama через OpenAI-совместимый интерфейс. В проект добавлен инструмент `search_review_kb`, который ищет правила ревью в локальной базе знаний по PEP, Ansible и внутреннему чеклисту.

## Что реализовано

- `app/tools/schemas.py`: Pydantic-модель аргументов и JSON Schema для `search_review_kb`; `description` подтягивается из `app/prompts/tools/search_review_kb.md`.
- `app/tools/handlers.py`: реальная функция-обработчик, которая читает `app/data/review_kb.json`, ищет релевантные фрагменты и возвращает структурированный результат.
- `app/prompts/system_v1.j2`: system prompt вынесен из клиента в отдельный файл и подгружается через `app/prompts/loader.py`.
- `app/prompts/few_shot_v1.md`: few-shot примеры вынесены в отдельный файл и автоматически подмешиваются в system prompt без правок кода.
- `app/llm/client.py`: полный цикл `messages + tools -> tool_call -> tool result -> second request -> final answer`, с поддержкой `openai` и `ollama`.
- `examples/run_tool_call.py`: скрипт для запуска трёх контрольных запросов.
- `tests/test_tool_call.py`: тесты на схему, обработчик и полный цикл через фейковый клиент.
- `logs/tool_call.log`: путь для журналирования каждого запуска.

## Структура

```text
app/
  config.py
  data/review_kb.json
  llm/client.py
  prompts/
    loader.py
    system_v1.j2
    tools/search_review_kb.md
  tools/
    handlers.py
    schemas.py
examples/run_tool_call.py
tests/test_tool_call.py
```

## Как запустить

1. Установить `uv`, если он ещё не установлен:

```bash
pip install uv
```

2. Синхронизировать зависимости проекта:

```bash
uv sync
```

3. Выбрать провайдера в `.env`.

Для OpenAI:

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-4.1-mini
```

Для Ollama:

```env
LLM_PROVIDER=ollama
OPENAI_MODEL=qwen3
OLLAMA_BASE_URL=http://localhost:11434/v1
```

Если проект запущен в devcontainer или Docker, по умолчанию можно использовать адрес `http://host.docker.internal:11434/v1`.

Перед запуском через Ollama нужно локально скачать модель, например:

```bash
ollama pull qwen3
```

4. Запустить пример:

```bash
uv run python examples/run_tool_call.py
```

5. Запустить тесты:

```bash
uv run python -m unittest discover -s tests -v
```

## Логирование

Каждый запуск пишет JSON-строки в `logs/tool_call.log` со следующими событиями:

- `user_input`
- `tool_call`
- `tool_result`
- `final_answer`

В логе сохраняются пользовательский ввод, имя и аргументы инструмента, результат функции, финальный ответ и `usage_total_tokens`.
