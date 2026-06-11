# Observability trace

`phoenix-trace.png` фиксирует пример trace из Phoenix UI для проекта
`diploma-fastapi`.

На скриншоте виден один `/chat` запрос со span `chat.request` и LLM-span
`ChatCompletion`, статусом `OK`, latency по trace, входом/выходом запроса и
связанными span-данными.

Ожидаемые атрибуты trace:

- `gen_ai.request.model`
- `gen_ai.usage.input_tokens`
- `gen_ai.usage.output_tokens`
