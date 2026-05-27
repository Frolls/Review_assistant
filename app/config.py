from functools import lru_cache
import os
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parent.parent


def default_ollama_base_url() -> str:
    if os.path.exists("/.dockerenv"):
        return "http://host.docker.internal:11434/v1"
    return "http://localhost:11434/v1"


class Settings(BaseSettings):
    llm_provider: Literal["openai", "ollama"] = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_base_url: str | None = None
    ollama_base_url: str = default_ollama_base_url()
    ollama_api_key: str = "ollama"
    product_name: str = "PR Review Bot"
    log_path: str = str(ROOT_DIR / "logs" / "tool_call.log")
    review_kb_path: str = str(ROOT_DIR / "app" / "data" / "review_kb.json")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
