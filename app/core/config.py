from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def default_openai_base_url() -> str:
    if os.path.exists("/.dockerenv"):
        return "http://host.docker.internal:4000"
    return "http://localhost:4000"


class LLMSettings(BaseModel):
    openai_api_key: SecretStr
    openai_base_url: str
    default_model: str
    request_timeout: float


class Settings(BaseSettings):
    openai_api_key: SecretStr = Field(validation_alias="OPENAI_API_KEY")
    ollama_base_url: str | None = Field(
        default=None,
        validation_alias="OLLAMA_BASE_URL",
        exclude=True,
    )
    openai_base_url: str = Field(
        default_factory=default_openai_base_url,
        validation_alias=AliasChoices("OPENAI_BASE_URL", "OLLAMA_BASE_URL"),
    )
    default_model: str = Field(
        default="gpt-4.1-mini",
        validation_alias=AliasChoices("DEFAULT_MODEL", "OPENAI_MODEL"),
    )
    request_timeout: float = Field(default=30.0, validation_alias="REQUEST_TIMEOUT")
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")
    cache_ttl_seconds: int = Field(default=300, validation_alias="CACHE_TTL_SECONDS")
    max_concurrency: int = Field(default=5, validation_alias="LLM_MAX_CONCURRENCY")
    rate_limit_per_min: int = Field(default=30, validation_alias="RATE_LIMIT_PER_MIN")
    security_guardrails_enabled: bool = Field(
        default=True,
        validation_alias="SECURITY_GUARDRAILS_ENABLED",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    observability_include_content: bool = Field(
        default=False,
        validation_alias="OBSERVABILITY_INCLUDE_CONTENT",
    )
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    chat_repository: Literal["json", "postgres"] = Field(
        default="json",
        validation_alias="CHAT_REPOSITORY",
    )
    chat_storage_dir: Path = Field(
        default=Path("./var/chats"),
        validation_alias="CHAT_STORAGE_DIR",
    )
    chat_context_strategy: Literal["sliding", "hybrid"] = Field(
        default="sliding",
        validation_alias="CHAT_CONTEXT_STRATEGY",
    )
    chat_context_window: int = Field(
        default=10,
        validation_alias="CHAT_CONTEXT_WINDOW",
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias="CORS_ORIGINS",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def apply_compatibility_defaults(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        openai_base_url = normalized.get("OPENAI_BASE_URL") or normalized.get("openai_base_url")
        ollama_base_url = normalized.get("OLLAMA_BASE_URL") or normalized.get("ollama_base_url")
        default_model = normalized.get("DEFAULT_MODEL") or normalized.get("default_model")
        openai_model = normalized.get("OPENAI_MODEL")
        redis_url = normalized.get("REDIS_URL") or normalized.get("redis_url")

        if (openai_base_url is None or str(openai_base_url).strip() == "") and ollama_base_url:
            normalized["OPENAI_BASE_URL"] = ollama_base_url

        if (default_model is None or str(default_model).strip() == "") and openai_model:
            normalized["DEFAULT_MODEL"] = openai_model

        if redis_url is None or str(redis_url).strip() == "":
            candidate_base_url = normalized.get("OPENAI_BASE_URL") or ollama_base_url
            if candidate_base_url:
                parsed = urlparse(str(candidate_base_url))
                if parsed.hostname:
                    normalized["REDIS_URL"] = f"redis://{parsed.hostname}:6379/0"

        return normalized

    @field_validator("openai_api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("OPENAI_API_KEY must not be blank")
        return value

    @field_validator("openai_base_url", mode="before")
    @classmethod
    def default_base_url(cls, value: object) -> object:
        if value is None or value == "":
            return default_openai_base_url()
        return value

    @field_validator("default_model", mode="before")
    @classmethod
    def default_model_name(cls, value: object) -> object:
        if value is None or value == "":
            return "gpt-4.1-mini"
        return value

    @field_validator("default_model")
    @classmethod
    def validate_default_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DEFAULT_MODEL must not be blank")
        return value

    @field_validator("request_timeout", mode="before")
    @classmethod
    def default_request_timeout(cls, value: object) -> object:
        if value is None or value == "":
            return 30.0
        return value

    @field_validator("request_timeout")
    @classmethod
    def validate_request_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("REQUEST_TIMEOUT must be greater than zero")
        return value

    @field_validator("redis_url", mode="before")
    @classmethod
    def default_redis_url(cls, value: object) -> object:
        if value is None or value == "":
            return "redis://localhost:6379/0"
        return value

    @field_validator("cache_ttl_seconds", mode="before")
    @classmethod
    def default_cache_ttl(cls, value: object) -> object:
        if value is None or value == "":
            return 300
        return value

    @field_validator("cache_ttl_seconds")
    @classmethod
    def validate_cache_ttl(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("CACHE_TTL_SECONDS must be greater than zero")
        return value

    @field_validator("max_concurrency", mode="before")
    @classmethod
    def default_max_concurrency(cls, value: object) -> object:
        if value is None or value == "":
            return 5
        return value

    @field_validator("max_concurrency")
    @classmethod
    def validate_max_concurrency(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("LLM_MAX_CONCURRENCY must be greater than zero")
        return value

    @field_validator("rate_limit_per_min", mode="before")
    @classmethod
    def default_rate_limit_per_min(cls, value: object) -> object:
        if value is None or value == "":
            return 30
        return value

    @field_validator("rate_limit_per_min")
    @classmethod
    def validate_rate_limit_per_min(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("RATE_LIMIT_PER_MIN must be greater than zero")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def default_log_level(cls, value: object) -> object:
        if value is None or value == "":
            return "INFO"
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("LOG_LEVEL must not be blank")
        return value.upper()

    @field_validator("chat_context_window", mode="before")
    @classmethod
    def default_chat_context_window(cls, value: object) -> object:
        if value is None or value == "":
            return 10
        return value

    @field_validator("chat_context_window")
    @classmethod
    def validate_chat_context_window(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("CHAT_CONTEXT_WINDOW must be greater than zero")
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            raw_value = value.strip()
            if not raw_value:
                return []
            if raw_value.startswith("["):
                parsed = json.loads(raw_value)
                if not isinstance(parsed, list):
                    raise ValueError("CORS_ORIGINS JSON value must be a list")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in raw_value.split(",") if item.strip()]
        raise ValueError("CORS_ORIGINS must be a list or a string")

    @property
    def llm(self) -> LLMSettings:
        return LLMSettings(
            openai_api_key=self.openai_api_key,
            openai_base_url=self.openai_base_url,
            default_model=self.default_model,
            request_timeout=self.request_timeout,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
