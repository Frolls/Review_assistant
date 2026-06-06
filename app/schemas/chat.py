from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1)


class Usage(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @classmethod
    def from_openai(cls, usage: Any) -> "Usage":
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", prompt_tokens + completion_tokens)
            or prompt_tokens + completion_tokens
        )
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    @classmethod
    def from_anthropic(cls, usage: Any) -> "Usage":
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        total_tokens = prompt_tokens + completion_tokens
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = Field(default=None, description="Overrides the configured default model.")
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=512, ge=1, le=16000)
    user_id: str | None = None
    session_id: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "messages": [
                        {"role": "system", "content": "You are a concise assistant."},
                        {"role": "user", "content": "Summarize FastAPI in one sentence."},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 128,
                    "user_id": "frontend-demo",
                    "session_id": "session-001",
                },
                {
                    "messages": [
                        {"role": "user", "content": "Считай до пяти и разделяй числа запятыми."}
                    ],
                    "model": "gpt-4.1-mini",
                    "temperature": 0.1,
                    "max_tokens": 64,
                },
            ]
        }
    )

    def with_default_model(self, default_model: str) -> "ChatRequest":
        return self.model_copy(update={"model": self.model or default_model})


class ChatResponse(BaseModel):
    content: str
    model: str
    usage: Usage
    finish_reason: str | None = None
    cached: bool = False

    @classmethod
    def from_openai(cls, response: Any, *, cached: bool = False) -> "ChatResponse":
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None

        content = _extract_text(message)
        model_name = str(getattr(response, "model", ""))
        usage = Usage.from_openai(getattr(response, "usage", None))

        return cls(
            content=content,
            model=model_name,
            usage=usage,
            finish_reason=finish_reason,
            cached=cached,
        )

    @classmethod
    def from_anthropic(cls, response: Any, *, cached: bool = False) -> "ChatResponse":
        content = _coerce_text(getattr(response, "content", None))
        model_name = str(getattr(response, "model", ""))
        usage = Usage.from_anthropic(getattr(response, "usage", None))
        finish_reason = getattr(response, "stop_reason", None)
        return cls(
            content=content,
            model=model_name,
            usage=usage,
            finish_reason=finish_reason,
            cached=cached,
        )


class ChatDelta(BaseModel):
    content: str | None = None
    usage: Usage | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "ChatDelta":
        if (self.content is None) == (self.usage is None):
            raise ValueError("ChatDelta must contain either content or usage")
        return self


class ValidationIssue(BaseModel):
    field: str
    message: str


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: list[ValidationIssue] | None = None


class ErrorResponse(BaseModel):
    error: ErrorInfo


def _extract_text(message: Any) -> str:
    if message is None:
        return ""
    for field_name in ("content", "reasoning", "reasoning_content", "text", "output_text"):
        text = _coerce_text(getattr(message, field_name, None))
        if text:
            return text
    return _coerce_text(message)


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_coerce_text(item) for item in value)
    if isinstance(value, dict):
        for field_name in ("text", "content", "reasoning", "output_text", "reasoning_content"):
            text = _coerce_text(value.get(field_name))
            if text:
                return text
        return ""
    for field_name in ("text", "content", "reasoning", "output_text", "reasoning_content"):
        if hasattr(value, field_name):
            text = _coerce_text(getattr(value, field_name))
            if text:
                return text
    return ""
