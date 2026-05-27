from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.prompts.loader import render_system_prompt
from app.tools.handlers import execute_tool
from app.tools.schemas import get_tools


def _setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("review_bot.tool_call")
    configured_path = getattr(logger, "_configured_path", None)
    if logger.handlers and configured_path == log_path:
        return logger

    if logger.handlers:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    logger._configured_path = log_path  # type: ignore[attr-defined]
    return logger


def _log_event(logger: logging.Logger, event: str, **payload: Any) -> None:
    logger.info(json.dumps({"event": event, **payload}, ensure_ascii=False))


def _usage_total_tokens(response: Any) -> int:
    usage = getattr(response, "usage", None)
    return int(getattr(usage, "total_tokens", 0) or 0)


def _assistant_message_to_dict(message: Any) -> dict[str, Any]:
    tool_calls = []
    for tool_call in getattr(message, "tool_calls", None) or []:
        tool_calls.append(
            {
                "id": tool_call.id,
                "type": getattr(tool_call, "type", "function"),
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
        )

    payload: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    if tool_calls:
        payload["tool_calls"] = tool_calls
    return payload


@dataclass
class ToolExecutionRecord:
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass
class RunResult:
    user_input: str
    final_text: str
    tool_used: bool
    tool_calls: list[ToolExecutionRecord]
    usage_total_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_input": self.user_input,
            "final_text": self.final_text,
            "tool_used": self.tool_used,
            "tool_calls": [asdict(record) for record in self.tool_calls],
            "usage_total_tokens": self.usage_total_tokens,
        }


class ReviewAssistantClient:
    def __init__(
        self,
        client: Any | None = None,
        settings: Settings | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or self._build_client()
        self.logger = logger or _setup_logger(self.settings.log_path)

    def _build_client_kwargs(self) -> dict[str, Any]:
        if self.settings.llm_provider == "ollama":
            return {
                "api_key": self.settings.ollama_api_key,
                "base_url": self.settings.ollama_base_url,
            }

        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env before running the client.")

        kwargs: dict[str, Any] = {"api_key": self.settings.openai_api_key}
        if self.settings.openai_base_url:
            kwargs["base_url"] = self.settings.openai_base_url
        return kwargs

    def _build_client(self) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "Package 'openai' is not installed. Install dependencies from pyproject.toml first."
            ) from exc

        return OpenAI(**self._build_client_kwargs())

    def _connection_error_message(self) -> str:
        if self.settings.llm_provider == "ollama":
            return (
                "Не удалось подключиться к Ollama. "
                f"Проверьте, что сервер запущен и доступен по адресу {self.settings.ollama_base_url}, "
                f"а модель {self.settings.openai_model} скачана. "
                "Обычно помогает запустить `ollama serve` и затем `ollama pull <имя_модели>`."
            )

        target = self.settings.openai_base_url or "https://api.openai.com/v1"
        return (
            "Не удалось подключиться к OpenAI API. "
            f"Проверьте доступность адреса {target} и корректность OPENAI_API_KEY."
        )

    def _create_completion(self, messages: list[dict[str, Any]]) -> Any:
        try:
            return self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=messages,
                tools=get_tools(),
            )
        except Exception as exc:
            try:
                from openai import APIConnectionError
            except ImportError:  # pragma: no cover
                APIConnectionError = Exception  # type: ignore[assignment]

            if isinstance(exc, APIConnectionError):
                raise RuntimeError(self._connection_error_message()) from exc
            raise

    def run_with_details(self, user_input: str) -> RunResult:
        system_prompt = render_system_prompt(product_name=self.settings.product_name)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        _log_event(self.logger, "user_input", text=user_input)
        first_response = self._create_completion(messages)
        first_message = first_response.choices[0].message
        first_tokens = _usage_total_tokens(first_response)
        tool_calls = getattr(first_message, "tool_calls", None) or []

        if not tool_calls:
            final_text = first_message.content or ""
            _log_event(
                self.logger,
                "final_answer",
                answer=final_text,
                usage_total_tokens=first_tokens,
                tool_used=False,
            )
            return RunResult(
                user_input=user_input,
                final_text=final_text,
                tool_used=False,
                tool_calls=[],
                usage_total_tokens=first_tokens,
            )

        messages.append(_assistant_message_to_dict(first_message))
        executed_tools: list[ToolExecutionRecord] = []

        for tool_call in tool_calls:
            raw_arguments = tool_call.function.arguments or "{}"
            arguments = json.loads(raw_arguments)
            _log_event(
                self.logger,
                "tool_call",
                tool_name=tool_call.function.name,
                arguments=arguments,
            )

            try:
                result = execute_tool(tool_call.function.name, arguments)
            except Exception as exc:  # pragma: no cover - defensive fallback
                result = {"error": str(exc)}

            _log_event(
                self.logger,
                "tool_result",
                tool_name=tool_call.function.name,
                result=result,
            )
            executed_tools.append(
                ToolExecutionRecord(
                    tool_name=tool_call.function.name,
                    arguments=arguments,
                    result=result,
                )
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        second_response = self._create_completion(messages)
        second_message = second_response.choices[0].message
        total_tokens = first_tokens + _usage_total_tokens(second_response)
        final_text = second_message.content or ""

        _log_event(
            self.logger,
            "final_answer",
            answer=final_text,
            usage_total_tokens=total_tokens,
            tool_used=True,
        )
        return RunResult(
            user_input=user_input,
            final_text=final_text,
            tool_used=True,
            tool_calls=executed_tools,
            usage_total_tokens=total_tokens,
        )

    def run(self, user_input: str) -> str:
        return self.run_with_details(user_input).final_text
