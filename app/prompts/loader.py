from functools import lru_cache
from pathlib import Path

from jinja2 import Template


PROMPTS_DIR = Path(__file__).resolve().parent


def read_prompt(relative_path: str) -> str:
    return (PROMPTS_DIR / relative_path).read_text(encoding="utf-8")


def read_optional_prompt(relative_path: str) -> str:
    path = PROMPTS_DIR / relative_path
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=8)
def load_tool_prompt(filename: str) -> str:
    return read_prompt(f"tools/{filename}")


@lru_cache(maxsize=8)
def load_few_shot_examples(version: str = "v1") -> str:
    return read_optional_prompt(f"few_shot_{version}.md")


@lru_cache(maxsize=8)
def render_system_prompt(version: str = "v1", **context: str) -> str:
    text = read_prompt(f"system_{version}.j2")
    if "few_shot_examples" not in context:
        context["few_shot_examples"] = load_few_shot_examples(version)
    return Template(text).render(**context)
