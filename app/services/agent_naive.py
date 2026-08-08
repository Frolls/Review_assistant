# This legacy baseline deliberately converts provider/tool failures into trace rows.
# ruff: noqa: BLE001, LOG015

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from openai import OpenAI

from app.core.config import get_settings


def search_knowledge_base(query: str) -> str:
    from app.services.rag import search_top_fragment

    return search_top_fragment(query)

def get_current_time(timezone: str = "Europe/Moscow") -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat()

def send_telegram_message(chat_id: str, text: str) -> str:
    print(f"[TELEGRAM → {chat_id}] {text}")
    return f"Сообщение отправлено в {chat_id}"

TOOLS = [
    {"type": "function", "function": {"name": "search_knowledge_base", "description": "Ищет сведения в базе знаний по ревью кода и возвращает наиболее релевантный фрагмент. Вызывай этот tool, когда ответ должен опираться на внутренние правила и документы проекта.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Поисковый запрос"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_current_time", "description": "Возвращает текущее время в указанной временной зоне в формате ISO 8601. Вызывай этот tool, когда для ответа или действия нужны реальные дата и время.", "parameters": {"type": "object", "properties": {"timezone": {"type": "string", "description": "IANA timezone", "default": "Europe/Moscow"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "send_telegram_message", "description": "Имитирует отправку текста в Telegram-чат и возвращает подтверждение без обращения к Telegram API. Вызывай этот tool только когда задача явно требует записать или отправить сообщение адресату.", "parameters": {"type": "object", "properties": {"chat_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["chat_id", "text"], "additionalProperties": False}}},
]
DISPATCH = {"search_knowledge_base": search_knowledge_base, "get_current_time": get_current_time, "send_telegram_message": send_telegram_message}

def _record(trace: list[dict], step: int, name: str | None, args: str, result: Any, usage: Any, started: float) -> None:
    duration = round((time.perf_counter() - started) * 1000, 2)
    trace.append({"step": step, "tool_name": name, "tool_args": args, "tool_result": str(result)[:200], "llm_input_tokens": getattr(usage, "prompt_tokens", 0), "llm_output_tokens": getattr(usage, "completion_tokens", 0), "duration_ms": duration})
    logging.info("agent step=%s tool=%s duration_ms=%s", step, name, duration)

def run_agent(task: str, max_steps: int = 6) -> dict:
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value(), base_url=settings.openai_base_url, timeout=settings.request_timeout)
    messages: list[Any] = [{"role": "system", "content": "Ты агент PR-review поддержки. Решай задачу доступными tools, не выдумывай функции и не повторяй без пользы запрос, по которому нет данных."}, {"role": "user", "content": task}]
    trace: list[dict[str, Any]] = []
    for step in range(max_steps):
        started = time.perf_counter()
        try:
            provider_options = (
                {"extra_body": {"think": False}}
                if "11434" in settings.openai_base_url
                else {}
            )
            response = client.chat.completions.create(
                model=settings.default_model,
                messages=messages,
                tools=TOOLS,
                temperature=0,
                **provider_options,
            )
        except Exception as exc:
            _record(trace, step + 1, None, "{}", exc, None, started)
            return {"answer": "Агент остановлен из-за ошибки LLM.", "steps": step + 1, "trace": trace, "error": str(exc)}
        message, usage = response.choices[0].message, response.usage
        messages.append(message)
        calls = message.tool_calls or []
        if not calls:
            answer = message.content or "Модель завершила работу без текстового ответа."
            _record(trace, step + 1, None, "{}", answer, usage, started)
            return {"answer": answer, "steps": step + 1, "trace": trace}
        for call in calls:
            raw_args, name = call.function.arguments or "{}", call.function.name
            try:
                args = json.loads(raw_args)
                result = DISPATCH[name](**args) if name in DISPATCH else f"Ошибка: tool {name!r} не разрешён. Доступны: {', '.join(DISPATCH)}"
            except Exception as exc:
                result = f"Ошибка выполнения tool {name!r}: {exc}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": str(result)})
            _record(trace, step + 1, name, raw_args, result, usage, started)
    return {"answer": f"Агент остановлен: достигнут лимит {max_steps} шагов.", "steps": max_steps, "trace": trace, "error": "max_steps_exceeded"}

def main() -> None:
    parser = argparse.ArgumentParser(description="Naive tool-calling agent")
    parser.add_argument("task")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    result = run_agent(args.task, args.max_steps)
    print(result["answer"])
    if args.trace:
        print(json.dumps(result["trace"], ensure_ascii=False, indent=2))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
