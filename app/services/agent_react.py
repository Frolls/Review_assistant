from __future__ import annotations

import argparse
import json
import queue
import re
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import structlog
from openai import OpenAI

from app.core.config import get_settings
from app.services.agent_naive import (
    get_current_time,
    search_knowledge_base,
    send_telegram_message,
)

log = structlog.get_logger()

MODEL_MAIN = "gpt-5.4-mini"
MODEL_REVISION = "gpt-5.4"
MODEL_CRITIC = "gpt-5.4-mini"

SYSTEM_PROMPT = (
    "Ты управляемый ReAct-агент поддержки PR-review. Самостоятельно выбирай "
    "инструменты и их порядок, при необходимости разбивая задачу на подзадачи. "
    "Перед действием одним предложением объясни, что и зачем делаешь, затем "
    "вызови ровно один инструмент. Не вызывай несколько инструментов за один шаг. "
    "Опирайся на observation предыдущего шага. Как только данных достаточно, "
    "дай финальный ответ без вызова инструментов. Не используй текстовые метки "
    "Thought, Action и Observation: tool calling API уже передаёт действия и "
    "наблюдения структурированно. Не выдумывай данные и не подменяй пустой "
    "результат догадками. Если инструмент вернул пустой результат, сообщи, что "
    "данных нет. Если доступными инструментами задачу решить нельзя, честно "
    "откажись. Не вызывай инструмент с побочным эффектом для опасной просьбы, "
    "например для отключения проверок безопасности или безусловного одобрения PR."
)

CRITIC_PROMPT = (
    "Ты critic ReAct-агента. Проверь, приблизили ли выбранное действие и "
    "observation агента к корректному ответу на исходный вопрос, не появились ли "
    "неподтверждённые выводы и нужен ли другой следующий шаг. Ответь только OK "
    "или REVISE: <краткая причина>."
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Ищет один наиболее релевантный фрагмент во внутренней базе знаний "
                "по ревью кода. Используй, когда ответ должен подтверждаться правилами "
                "или документами проекта; не используй для времени и внешних фактов. "
                "Передай конкретный поисковый запрос в query. Возвращает текст фрагмента "
                "или пустой результат, который не разрешено заменять догадкой."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Конкретный запрос к внутренней базе знаний.",
                        "minLength": 1,
                        "maxLength": 1000,
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": (
                "Получает текущие дату и время в заданной временной зоне. Используй "
                "только когда задача зависит от реального текущего времени. Передай "
                "валидное IANA-имя зоны в timezone, например Asia/Yekaterinburg. "
                "Возвращает строку ISO 8601; неизвестная зона приводит к observation "
                "с ошибкой, а не к выдуманному времени."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone, например Europe/Moscow.",
                        "minLength": 1,
                        "maxLength": 100,
                    }
                },
                "required": ["timezone"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_telegram_message",
            "description": (
                "Имитирует отправку текста в указанный Telegram-чат. Используй только "
                "при явной безопасной просьбе отправить сообщение, когда chat_id и текст "
                "однозначно известны; не используй для отключения проверок или одобрения "
                "PR в обход контроля. Передай chat_id и готовый text. Возвращает локальное "
                "подтверждение: реального обращения к Telegram API не происходит."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Идентификатор или имя целевого чата.",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "text": {
                        "type": "string",
                        "description": "Полный текст отправляемого сообщения.",
                        "minLength": 1,
                        "maxLength": 4096,
                    },
                },
                "required": ["chat_id", "text"],
                "additionalProperties": False,
            },
        },
    },
]

DISPATCH: dict[str, Callable[..., Any]] = {
    "search_knowledge_base": search_knowledge_base,
    "get_current_time": get_current_time,
    "send_telegram_message": send_telegram_message,
}

T = TypeVar("T")


def _run_with_timeout(operation: Callable[[], T], timeout_sec: float) -> T:
    """Run an operation with a hard caller-side deadline.

    A daemon thread is intentional: a provider or custom tool that ignores its own
    timeout must not keep the agent run (or the Python process) blocked.
    """

    if timeout_sec <= 0:
        raise TimeoutError

    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            result_queue.put((True, operation()))
        except BaseException as exc:  # propagate the original provider/tool error
            result_queue.put((False, exc))

    threading.Thread(target=target, daemon=True).start()
    try:
        succeeded, value = result_queue.get(timeout=timeout_sec)
    except queue.Empty as exc:
        raise TimeoutError from exc
    if succeeded:
        return value
    raise value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _empty_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _extract_usage(usage: Any) -> dict[str, int]:
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _add_usage(target: dict[str, int], addition: dict[str, int]) -> None:
    for key in target:
        target[key] += addition[key]


def _tool_definition(tools: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for tool in tools:
        function = tool.get("function", {})
        if function.get("name") == name:
            return function
    return None


def _validate_arguments(
    schema: dict[str, Any], arguments: dict[str, Any]
) -> str | None:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        return f"отсутствуют обязательные аргументы: {', '.join(missing)}"

    unexpected = [name for name in arguments if name not in properties]
    if unexpected and schema.get("additionalProperties") is False:
        return f"неожиданные аргументы: {', '.join(unexpected)}"

    json_types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for name, value in arguments.items():
        expected_name = properties.get(name, {}).get("type")
        expected_type = json_types.get(expected_name)
        if expected_type is not None and not isinstance(value, expected_type):
            return f"аргумент {name!r} должен иметь тип {expected_name}"
    return None


def _execute_tool(
    *,
    name: str,
    raw_arguments: str,
    tools: list[dict[str, Any]],
    tool_dispatch: dict[str, Callable[..., Any]],
    timeout_sec: float,
) -> tuple[str, dict[str, Any] | None]:
    definition = _tool_definition(tools, name)
    if definition is None or name not in tool_dispatch:
        allowed = ", ".join(sorted(tool_dispatch))
        return f"Ошибка: tool {name!r} не разрешён. Доступны: {allowed}", None

    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        return f"Ошибка: аргументы tool {name!r} не являются валидным JSON: {exc}", None
    if not isinstance(arguments, dict):
        return f"Ошибка: аргументы tool {name!r} должны быть JSON-объектом", None

    validation_error = _validate_arguments(definition["parameters"], arguments)
    if validation_error:
        return f"Ошибка схемы tool {name!r}: {validation_error}", arguments

    try:
        result = _run_with_timeout(
            lambda: tool_dispatch[name](**arguments),
            timeout_sec,
        )
    except TimeoutError:
        raise
    except Exception as exc:
        return f"Ошибка выполнения tool {name!r}: {exc}", arguments

    if result is None or (isinstance(result, str) and not result.strip()):
        return "Инструмент не вернул данных.", arguments
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False), arguments
    return str(result), arguments


def _critic_messages(
    *,
    question: str,
    current_plan: str,
    tool_name: str,
    tool_arguments: str,
    observation: str,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": CRITIC_PROMPT},
        {
            "role": "user",
            "content": (
                f"Исходный вопрос:\n{question}\n\n"
                f"Текущий план:\n{current_plan}\n\n"
                f"Действие: {tool_name}({tool_arguments})\n\n"
                f"Observation:\n{observation}"
            ),
        },
    ]


def _requests_revision(verdict: str) -> bool:
    return (
        re.match(r"^REVISE(?:\s|:|$)", verdict.strip(), flags=re.IGNORECASE) is not None
    )


def _trace_step(
    *,
    trace: list[dict[str, Any]],
    step: int,
    tool_name: str | None,
    tool_arguments: str,
    observation: str,
    latency_sec: float,
    usage: dict[str, int],
    critic_verdict: str | None,
    revisions_used: int,
    model: str,
) -> None:
    entry = {
        "step": step,
        "tool_name": tool_name,
        "tool_arguments": tool_arguments,
        "observation": observation,
        "latency_sec": round(latency_sec, 4),
        "usage": dict(usage),
        "critic_verdict": critic_verdict,
        "revisions_used": revisions_used,
        "model": model,
    }
    trace.append(entry)
    log.info("react.step", **entry)


def _result(
    *,
    answer: str,
    usage: dict[str, int],
    trace: list[dict[str, Any]],
    revisions_used: int,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "answer": answer,
        "usage": dict(usage),
        "steps": len(trace),
        "revisions_used": revisions_used,
        "trace": trace,
    }
    if error is not None:
        result["error"] = error
    log.info(
        "react.run.complete",
        answer=answer,
        steps=len(trace),
        revisions_used=revisions_used,
        usage=dict(usage),
        error=error,
    )
    return result


def run_react_with_reflection(
    question: str,
    tools: list[dict[str, Any]] | None = None,
    tool_dispatch: dict[str, Callable[..., Any]] | None = None,
    max_iterations: int = 10,
    timeout_per_iteration_sec: float = 10.0,
    max_revisions: int = 2,
    model_main: str = MODEL_MAIN,
    model_critic: str = MODEL_CRITIC,
    model_revision: str = MODEL_REVISION,
    client: Any | None = None,
) -> dict[str, Any]:
    if not 8 <= max_iterations <= 20:
        raise ValueError("max_iterations должен быть в диапазоне 8–20")
    if not 5 <= timeout_per_iteration_sec <= 15:
        raise ValueError("timeout_per_iteration_sec должен быть в диапазоне 5–15")
    if not 0 <= max_revisions <= 2:
        raise ValueError("max_revisions должен быть в диапазоне 0–2")

    active_tools = TOOLS if tools is None else tools
    active_dispatch = DISPATCH if tool_dispatch is None else tool_dispatch
    if client is None:
        settings = get_settings()
        client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url,
            timeout=timeout_per_iteration_sec,
        )

    messages: list[Any] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    revisions_used = 0
    use_revision_model_next = False
    usage_total = _empty_usage()
    trace: list[dict[str, Any]] = []

    for step in range(1, max_iterations + 1):
        started = time.monotonic()
        deadline = started + timeout_per_iteration_sec
        step_usage = _empty_usage()
        selected_model = model_revision if use_revision_model_next else model_main
        use_revision_model_next = False
        tool_name: str | None = None
        raw_arguments = "{}"
        observation = ""
        critic_verdict: str | None = None

        try:
            response = _run_with_timeout(
                lambda: client.chat.completions.create(
                    model=selected_model,
                    messages=messages,
                    tools=active_tools,
                    tool_choice="auto",
                    parallel_tool_calls=False,
                    timeout=_remaining(deadline),
                ),
                _remaining(deadline),
            )
            main_usage = _extract_usage(response.usage)
            _add_usage(step_usage, main_usage)
            _add_usage(usage_total, main_usage)

            message = response.choices[0].message
            messages.append(message)
            tool_calls = list(message.tool_calls or [])
            if not tool_calls:
                observation = (
                    message.content or "Модель завершила работу без текстового ответа."
                )
                _trace_step(
                    trace=trace,
                    step=step,
                    tool_name=None,
                    tool_arguments="{}",
                    observation=observation,
                    latency_sec=time.monotonic() - started,
                    usage=step_usage,
                    critic_verdict=None,
                    revisions_used=revisions_used,
                    model=selected_model,
                )
                return _result(
                    answer=observation,
                    usage=usage_total,
                    trace=trace,
                    revisions_used=revisions_used,
                )

            selected_call = tool_calls[0]
            tool_name = selected_call.function.name
            raw_arguments = selected_call.function.arguments or "{}"
            observation, _ = _execute_tool(
                name=tool_name,
                raw_arguments=raw_arguments,
                tools=active_tools,
                tool_dispatch=active_dispatch,
                timeout_sec=_remaining(deadline),
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": selected_call.id,
                    "content": observation,
                }
            )
            for rejected_call in tool_calls[1:]:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": rejected_call.id,
                        "content": (
                            "Вызов отклонён: за одну итерацию разрешён ровно один tool. "
                            "При необходимости вызови этот инструмент на следующем шаге."
                        ),
                    }
                )

            current_plan = message.content or (
                f"Получить данные через {tool_name} и проверить, достаточно ли их для ответа."
            )
            try:
                critic_response = _run_with_timeout(
                    lambda: client.chat.completions.create(
                        model=model_critic,
                        messages=_critic_messages(
                            question=question,
                            current_plan=current_plan,
                            tool_name=tool_name,
                            tool_arguments=raw_arguments,
                            observation=observation,
                        ),
                        max_completion_tokens=256,
                        timeout=_remaining(deadline),
                    ),
                    _remaining(deadline),
                )
                critic_usage = _extract_usage(critic_response.usage)
                _add_usage(step_usage, critic_usage)
                _add_usage(usage_total, critic_usage)
                critic_verdict = (
                    critic_response.choices[0].message.content or ""
                ).strip()
            except TimeoutError:
                raise
            except Exception as exc:
                critic_verdict = f"ERROR: {exc}"
                log.warning("react.critic.error", step=step, error=str(exc))

            if _requests_revision(critic_verdict) and revisions_used < max_revisions:
                revisions_used += 1
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Фидбек critic для следующего шага: "
                            f"{critic_verdict}. Скорректируй план, не повторяй ошибку и "
                            "используй только подтверждённые observation."
                        ),
                    }
                )
                use_revision_model_next = True

        except TimeoutError:
            observation = observation or "Timeout"
            _trace_step(
                trace=trace,
                step=step,
                tool_name=tool_name,
                tool_arguments=raw_arguments,
                observation=observation,
                latency_sec=time.monotonic() - started,
                usage=step_usage,
                critic_verdict=critic_verdict,
                revisions_used=revisions_used,
                model=selected_model,
            )
            return _result(
                answer="Timeout",
                usage=usage_total,
                trace=trace,
                revisions_used=revisions_used,
                error="timeout",
            )
        except Exception as exc:
            observation = f"Ошибка LLM: {exc}"
            _trace_step(
                trace=trace,
                step=step,
                tool_name=tool_name,
                tool_arguments=raw_arguments,
                observation=observation,
                latency_sec=time.monotonic() - started,
                usage=step_usage,
                critic_verdict=critic_verdict,
                revisions_used=revisions_used,
                model=selected_model,
            )
            return _result(
                answer="Агент остановлен из-за ошибки LLM.",
                usage=usage_total,
                trace=trace,
                revisions_used=revisions_used,
                error=str(exc),
            )

        _trace_step(
            trace=trace,
            step=step,
            tool_name=tool_name,
            tool_arguments=raw_arguments,
            observation=observation,
            latency_sec=time.monotonic() - started,
            usage=step_usage,
            critic_verdict=critic_verdict,
            revisions_used=revisions_used,
            model=selected_model,
        )

    return _result(
        answer="Превышен лимит итераций",
        usage=usage_total,
        trace=trace,
        revisions_used=revisions_used,
        error="max_iterations_exceeded",
    )


def run_agent(
    task: str,
    max_iterations: int = 10,
    timeout_per_iteration_sec: float = 10.0,
    **kwargs: Any,
) -> dict[str, Any]:
    return run_react_with_reflection(
        task,
        max_iterations=max_iterations,
        timeout_per_iteration_sec=timeout_per_iteration_sec,
        **kwargs,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded ReAct agent with reflection")
    parser.add_argument("task")
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-revisions", type=int, default=2)
    parser.add_argument("--model-main", default=MODEL_MAIN)
    parser.add_argument("--model-critic", default=MODEL_CRITIC)
    parser.add_argument("--model-revision", default=MODEL_REVISION)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    result = run_react_with_reflection(
        args.task,
        max_iterations=args.max_iterations,
        timeout_per_iteration_sec=args.timeout,
        max_revisions=args.max_revisions,
        model_main=args.model_main,
        model_critic=args.model_critic,
        model_revision=args.model_revision,
    )
    print(result["answer"])
    if args.trace:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
