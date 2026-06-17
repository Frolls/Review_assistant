from __future__ import annotations

# ruff: noqa: E402

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings
from app.llm.parsing import parse_json_object
from app.prompts.review import build_review_messages
from app.schemas.chat import ChatRequest
from app.services.llm import LLMService


class NoopCache:
    async def get(self, key: str) -> None:
        return None

    async def setex(self, key: str, ttl: int, value: str) -> None:
        return None


JUDGE_SYSTEM_PROMPT = (
    "/no_think\n"
    "Ты беспристрастный LLM-судья для оценки ответов ИИ-ассистента по ревью кода. "
    "Используй подход reason-then-score: сначала рассуждение, затем оценки. "
    "Верни только валидный JSON."
)

JUDGE_USER_TEMPLATE = """/no_think
Оцени ответ ассистента относительно эталонного ответа.

Шаги оценки:
1. Сначала напиши краткое reasoning: максимум 2 предложения, сравни ответ ассистента с эталоном.
2. Затем выставь целочисленные оценки от 1 до 5 по критериям relevance, correctness и completeness.
3. Затем напиши explanation в одну строку.

Верни JSON object с полями строго в таком порядке:
reasoning, scores, explanation.
Не добавляй markdown, дополнительные поля или длинное рассуждение.

Пример JSON:
{{
  "reasoning": "Ответ называет правильную проблему и объясняет риск, но не предлагает тест.",
  "scores": {{
    "relevance": 5,
    "correctness": 4,
    "completeness": 3
  }},
  "explanation": "В целом верно, но есть важный пропуск."
}}

Вопрос:
{question}

Эталонный ответ:
{expected_answer}

Ожидаемые ключевые слова или синонимы:
{expected_keywords}

Запрещённые слова или утверждения:
{must_not_contain}

Ответ ассистента:
{answer}
"""


async def main() -> int:
    args = parse_args()
    golden = load_golden(args.golden)
    settings = Settings()
    model_under_test = args.model or settings.default_model
    out_path = args.out or default_out_path()
    proxy = args.proxy or os.getenv("EVAL_PROXY")

    client = build_openai_client(settings=settings, proxy=proxy)
    service = LLMService(openai=client, cache=NoopCache(), settings=settings)

    try:
        if proxy:
            print(f"Using eval proxy: {mask_proxy_url(proxy)}")
            if not args.skip_proxy_check:
                await check_proxy_connectivity(proxy=proxy, url=args.preflight_url)
        golden_items = active_golden_items(golden)
        if args.limit is not None:
            golden_items = golden_items[: args.limit]

        items = []
        total_items = len(golden_items)
        for index, item in enumerate(golden_items, start=1):
            print(f"[{index}/{total_items}] answering {item['id']}", flush=True)
            answer = await answer_item(
                service=service,
                item=item,
                model=model_under_test,
                max_tokens=args.max_tokens,
            )
            print(f"[{index}/{total_items}] judging {item['id']}", flush=True)
            judgement = await judge_item(
                client=client,
                item=item,
                answer=answer,
                judge_model=args.judge,
                max_tokens=args.judge_max_tokens,
            )
            items.append(
                {
                    "id": item["id"],
                    "question": item["question"],
                    "answer": answer,
                    "scores": judgement["scores"],
                    "reasoning": judgement["reasoning"],
                    "explanation": judgement["explanation"],
                }
            )

        run = build_run_artifact(
            items=items,
            golden_version=golden["version"],
            model_under_test=model_under_test,
            judge_model=args.judge,
        )
        write_json(out_path, run)
        print(f"Wrote evaluation run to {out_path}")
        print(json.dumps(run["aggregates"], ensure_ascii=False, indent=2))
        return 0
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline golden evaluation.")
    parser.add_argument("--golden", default="eval/golden_dataset.json")
    parser.add_argument("--judge", default="gpt-5.2")
    parser.add_argument("--model", default=None, help="Model under test. Defaults to DEFAULT_MODEL.")
    parser.add_argument("--out", default=None, help="Output JSON path. Defaults to eval/runs/YYYY-MM-DD.json.")
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--judge-max-tokens", type=int, default=500)
    parser.add_argument("--limit", type=int, default=None, help="Evaluate only the first N active items.")
    parser.add_argument(
        "--proxy",
        default=None,
        help="Optional HTTP/SOCKS proxy URL. Prefer EVAL_PROXY to avoid shell history leaks.",
    )
    parser.add_argument(
        "--preflight-url",
        default="https://api.ipify.org?format=json",
        help="URL used to verify proxy connectivity before running eval.",
    )
    parser.add_argument(
        "--skip-proxy-check",
        action="store_true",
        help="Skip proxy connectivity preflight.",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    return args


def build_openai_client(*, settings: Settings, proxy: str | None) -> AsyncOpenAI:
    kwargs: dict[str, Any] = {
        "api_key": settings.openai_api_key.get_secret_value(),
        "base_url": settings.openai_base_url,
        "timeout": settings.request_timeout,
    }
    if proxy:
        validate_proxy_url(proxy)
        kwargs["http_client"] = httpx.AsyncClient(
            proxy=proxy,
            timeout=settings.request_timeout,
        )
    return AsyncOpenAI(**kwargs)


def validate_proxy_url(proxy: str) -> None:
    parsed = urlsplit(proxy)
    if parsed.scheme in {"http", "https"} and parsed.port in {1080, 1081}:
        raise RuntimeError(
            "Proxy URL looks like an HTTP proxy pointed at a common SOCKS port. "
            "Use the HTTP proxy port, for example http://...:8888, or use "
            "socks5://...:1081 with httpx socks support installed."
        )
    if proxy.startswith(("socks5://", "socks5h://")) and importlib.util.find_spec("socksio") is None:
        raise RuntimeError(
            "SOCKS proxy support requires installing the httpx socks extra. "
            "Use an HTTP proxy or add httpx[socks]."
        )


async def check_proxy_connectivity(*, proxy: str, url: str) -> None:
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=10) as http:
            response = await http.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Proxy preflight failed for {mask_proxy_url(proxy)} via {url}: {exc}"
        ) from exc


def mask_proxy_url(proxy: str) -> str:
    parsed = urlsplit(proxy)
    if not parsed.username and not parsed.password:
        return proxy

    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if parsed.username:
        auth = f"{parsed.username}:***@"
    else:
        auth = "***@"
    return urlunsplit((parsed.scheme, f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))


def load_golden(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as fh:
        golden = json.load(fh)

    if golden.get("version") is None:
        raise ValueError("Golden dataset must contain top-level version")
    items = golden.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("Golden dataset must contain non-empty items list")
    for item in items:
        if not isinstance(item.get("deprecated", False), bool):
            raise ValueError(f"Golden item {item.get('id', '<unknown>')} has non-boolean deprecated")
    return golden


def active_golden_items(golden: dict[str, Any]) -> list[dict[str, Any]]:
    items = [item for item in golden["items"] if not item.get("deprecated", False)]
    if not items:
        raise ValueError("Golden dataset has no active items")
    return items


async def answer_item(
    *,
    service: LLMService,
    item: dict[str, Any],
    model: str,
    max_tokens: int,
) -> str:
    request = ChatRequest(
        messages=build_review_messages(item["question"]),
        model=model,
        temperature=0,
        max_tokens=max_tokens,
    )
    response = await service.complete(request)
    return response.content


async def judge_item(
    *,
    client: AsyncOpenAI,
    item: dict[str, Any],
    answer: str,
    judge_model: str,
    max_tokens: int,
) -> dict[str, Any]:
    prompt = JUDGE_USER_TEMPLATE.format(
        question=item["question"],
        expected_answer=item["expected_answer"],
        expected_keywords=", ".join(item.get("expected_keywords", [])),
        must_not_contain=", ".join(item.get("must_not_contain", [])),
        answer=answer,
    )
    response = await client.chat.completions.create(
        model=judge_model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    parsed = parse_json_object(content)
    return normalize_judgement(parsed)


def normalize_judgement(parsed: dict[str, Any]) -> dict[str, Any]:
    scores = parsed.get("scores")
    if scores is None and all(
        key in parsed for key in ("relevance", "correctness", "completeness")
    ):
        scores = parsed
    if not isinstance(scores, dict):
        raise ValueError(
            "Judge response must contain scores object; "
            f"got top-level keys: {', '.join(parsed.keys())}"
        )

    normalized_scores = {
        "relevance": _score(scores, "relevance"),
        "correctness": _score(scores, "correctness"),
        "completeness": _score(scores, "completeness"),
    }
    reasoning = str(parsed.get("reasoning", "")).strip()
    explanation = str(parsed.get("explanation", "")).strip()
    if not reasoning:
        raise ValueError("Judge response must contain reasoning before scores")
    return {
        "reasoning": reasoning,
        "scores": normalized_scores,
        "explanation": explanation,
    }


def _score(scores: dict[str, Any], key: str) -> int:
    value = int(scores[key])
    if value < 1 or value > 5:
        raise ValueError(f"Judge score {key} must be between 1 and 5")
    return value


def build_run_artifact(
    *,
    items: list[dict[str, Any]],
    golden_version: int,
    model_under_test: str,
    judge_model: str,
) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat()
    return {
        "run_id": f"eval-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
        "timestamp": timestamp,
        "model_under_test": model_under_test,
        "judge_model": judge_model,
        "golden_version": golden_version,
        "items": items,
        "aggregates": aggregate(items),
    }


def aggregate(items: list[dict[str, Any]]) -> dict[str, float]:
    if not items:
        raise ValueError("Cannot aggregate an empty evaluation run")

    relevance = [item["scores"]["relevance"] for item in items]
    correctness = [item["scores"]["correctness"] for item in items]
    completeness = [item["scores"]["completeness"] for item in items]
    return {
        "relevance_avg": round(sum(relevance) / len(relevance), 3),
        "correctness_avg": round(sum(correctness) / len(correctness), 3),
        "completeness_avg": round(sum(completeness) / len(completeness), 3),
        "min_correctness": min(correctness),
    }


def default_out_path() -> str:
    return f"eval/runs/{datetime.now(UTC).strftime('%Y-%m-%d')}.json"


def write_json(path: str, payload: dict[str, Any]) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
