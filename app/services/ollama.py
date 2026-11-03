"""Supported parameters for Ollama's OpenAI-compatible endpoint.

The compatibility API ignores options.num_ctx. Create a derived local model
with that parameter instead; never modify the user's original model.
"""
import hashlib
import re
from urllib.parse import urlsplit

import httpx

from app.observability.logging import get_logger

logger = get_logger(__name__)


def is_ollama(base_url: str) -> bool:
    url = urlsplit(str(base_url))
    return url.port == 11434 or "ollama" in (url.hostname or "").lower()


def model_options(base_url: str) -> dict:
    return {"reasoning_effort": "none"} if is_ollama(base_url) else {}


async def configure_context(settings, *, transport=None) -> None:
    if not is_ollama(settings.openai_base_url):
        return
    settings.llm_num_ctx = settings.llm_num_ctx or 8192
    base = settings.openai_base_url.rstrip("/").removesuffix("/v1")
    async with httpx.AsyncClient(base_url=base, timeout=settings.request_timeout,
                                transport=transport) as client:
        response = await client.get("/api/tags")
        response.raise_for_status()
        installed = {m["name"]: m for m in response.json()["models"]}
        resolved = {}
        for field in ("default_model", "vision_model", "review_model"):
            name = getattr(settings, field, None)
            if not name:
                continue
            if name in resolved:
                setattr(settings, field, resolved[name])
                continue
            canonical = name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"
            model = installed.get(canonical)
            if model is None:
                raise RuntimeError(f"Ollama model {name!r} is not installed; no automatic download performed")
            shown = await client.post("/api/show", json={"model": canonical})
            shown.raise_for_status()
            parameters = shown.json().get("parameters", "")
            context = re.search(r"^num_ctx\s+(\d+)\s*$", parameters, re.M)
            if context and int(context[1]) == settings.llm_num_ctx:
                resolved[name] = name
                continue
            identity = hashlib.sha256(f"{model['digest']}:{settings.llm_num_ctx}".encode()).hexdigest()[:16]
            alias = f"review-context-{identity}:ctx{settings.llm_num_ctx}"
            if alias not in installed:
                created = await client.post("/api/create", json={
                    "model": alias, "from": canonical,
                    "parameters": {"num_ctx": settings.llm_num_ctx}, "stream": False,
                })
                created.raise_for_status()
                if created.json().get("status") != "success":
                    raise RuntimeError("Ollama did not confirm model creation")
            verified = await client.post("/api/show", json={"model": alias})
            verified.raise_for_status()
            match = re.search(r"^num_ctx\s+(\d+)\s*$", verified.json().get("parameters", ""), re.M)
            if not match or int(match[1]) != settings.llm_num_ctx:
                raise RuntimeError("Ollama model context does not match LLM_NUM_CTX")
            setattr(settings, field, alias)
            resolved[name] = alias
            logger.info("ollama.context_configured", original_model=name, model=alias,
                        context_tokens=settings.llm_num_ctx)
