from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from app.core.config import Settings, get_settings
from app.services.rag import RAGService, UNKNOWN_ANSWER


ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = ROOT / "experiments" / "results.json"


@dataclass(frozen=True, slots=True)
class TestQuestion:
    id: str
    category: str
    question: str
    reference: str


TEST_QUESTIONS = (
    TestQuestion(
        id="corpus-1",
        category="corpus",
        question=(
            "Почему в Ansible task лучше избегать command и shell и как обеспечить "
            "идемпотентность, если shell всё же неизбежен?"
        ),
        reference=(
            "Следует предпочитать специализированные идемпотентные модули. Для неизбежного "
            "shell нужны creates, removes, явная проверка состояния или корректный changed_when."
        ),
    ),
    TestQuestion(
        id="corpus-2",
        category="corpus",
        question="Что должен доказать тест для исправления production-бага в PR?",
        reference=(
            "Нужен минимальный регрессионный тест, который падает до исправления и проходит "
            "после него, проверяя наблюдаемое поведение."
        ),
    ),
    TestQuestion(
        id="corpus-3",
        category="corpus",
        question=(
            "Какие требования безопасности предъявить на ревью к новому outbound HTTP-клиенту?"
        ),
        reference=(
            "Нужны timeout, retry только для идемпотентных операций или с защитой от повтора "
            "side effect, а для внешних URL — allowlist доменов против SSRF."
        ),
    ),
    TestQuestion(
        id="multi-step-1",
        category="multi-step",
        question=(
            "Составь единый review-комментарий для PR: внешний JSON после cast() передают в "
            "сервис, сервис зависит от конкретного PostgresRepository, а новый HTTP-клиент "
            "повторяет запросы без timeout. Объясни риски и предложи исправления."
        ),
        reference=(
            "cast не валидирует внешний JSON во время исполнения, поэтому нужен runtime parser "
            "или validator; сервису полезнее зависеть от минимального Protocol репозитория; "
            "HTTP-клиенту нужен timeout, а retry допустим только при идемпотентности или защите "
            "от повторного side effect."
        ),
    ),
    TestQuestion(
        id="out-of-base-1",
        category="out-of-base",
        question="Какова ставка НДС в Казахстане в 2026 году?",
        reference=UNKNOWN_ANSWER,
    ),
)


class UsageTracker(BaseCallbackHandler):
    """Aggregate token usage once for every completed chat-model call."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.llm_calls = 0
        self._lock = Lock()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        usage = _usage_from_result(response)
        with self._lock:
            self.llm_calls += 1
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]
            self.total_tokens += usage["total_tokens"]

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.llm_calls,
        }


_rag_service: RAGService | None = None


def experiment_settings() -> Settings:
    settings = get_settings()
    base_url = settings.openai_base_url.replace("host.docker.internal", "localhost")
    return settings.model_copy(update={"openai_base_url": base_url})


def experiment_model(*, model_name: str | None = None) -> ChatOpenAI:
    settings = experiment_settings()
    kwargs: dict[str, Any] = {
        "model": model_name or settings.default_model,
        "api_key": settings.openai_api_key,
        "base_url": settings.openai_base_url,
        "temperature": 0,
        "timeout": max(settings.request_timeout, 180),
        "max_retries": 0,
    }
    if "11434" in settings.openai_base_url or "ollama" in settings.openai_base_url.casefold():
        kwargs["extra_body"] = {"think": False}
    return ChatOpenAI(**kwargs)


async def get_rag_service() -> RAGService:
    global _rag_service
    if _rag_service is None:
        _rag_service = RAGService(experiment_settings())
        await _rag_service.build()
    return _rag_service


async def close_rag_service() -> None:
    global _rag_service
    if _rag_service is not None:
        await _rag_service.close()
        _rag_service = None


@tool
async def search_knowledge_base(query: str) -> str:
    """Search the review knowledge base and return grounded numbered source fragments."""

    service = await get_rag_service()
    prepared = await service.prepare(query)
    if not prepared.confident:
        payload = {
            "confident": False,
            "answer": UNKNOWN_ANSWER,
            "top_score": prepared.top_score,
            "sources": [],
        }
    else:
        payload = {
            "confident": True,
            "top_score": prepared.top_score,
            "sources": [
                {
                    "id": source["id"],
                    "file_name": source["file_name"],
                    "page": source["page"],
                    "score": source["score"],
                    "text": full_text,
                }
                for source, full_text in zip(prepared.sources, prepared.retrieved_contexts)
            ],
        }
    return json.dumps(payload, ensure_ascii=False)


def final_ai_text(result: dict[str, Any]) -> str:
    for message in reversed(result.get("messages", [])):
        if isinstance(message, AIMessage) and message.content:
            return _message_text(message.content).strip()
    return ""


def make_result(
    *,
    implementation: str,
    test: TestQuestion,
    answer: str,
    tracker: UsageTracker,
    latency_ms: float,
    handoff_count: int,
) -> dict[str, Any]:
    return {
        "implementation": implementation,
        "question_id": test.id,
        "category": test.category,
        "question": test.question,
        "answer": answer,
        **tracker.as_dict(),
        "latency_ms": round(latency_ms, 3),
        "handoff_count": handoff_count,
        "quality_score": None,
        "quality_reason": None,
        "model": experiment_settings().default_model,
        "measured_at": datetime.now(UTC).isoformat(),
    }


def save_results(implementation: str, new_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = load_results()
    records = [row for row in existing if row.get("implementation") != implementation]
    records.extend(new_records)
    order = {test.id: index for index, test in enumerate(TEST_QUESTIONS)}
    implementation_order = {"single-agent": 0, "multi-agent": 1}
    records.sort(
        key=lambda row: (
            order.get(str(row.get("question_id")), 999),
            implementation_order.get(str(row.get("implementation")), 999),
        )
    )
    RESULTS_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return records


def load_results() -> list[dict[str, Any]]:
    if not RESULTS_PATH.exists():
        return []
    data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("results", [])


async def judge_results_if_complete() -> bool:
    """Blind-score both complete result sets in one unmetered judge call."""

    records = load_results()
    expected_keys = {
        (implementation, test.id)
        for implementation in ("single-agent", "multi-agent")
        for test in TEST_QUESTIONS
    }
    actual_keys = {
        (str(row.get("implementation")), str(row.get("question_id"))) for row in records
    }
    if not expected_keys.issubset(actual_keys):
        return False

    references = {test.id: test.reference for test in TEST_QUESTIONS}
    candidates = []
    candidate_to_key: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(records):
        candidate_id = f"candidate-{index + 1}"
        key = (str(row["implementation"]), str(row["question_id"]))
        candidate_to_key[candidate_id] = key
        candidates.append(
            {
                "candidate_id": candidate_id,
                "question": row["question"],
                "reference": references[key[1]],
                "answer": row["answer"],
            }
        )

    judge_model = os.getenv("MULTI_AGENT_JUDGE_MODEL", "qwen2.5:14b")
    prompt = (
        "Ты независимый LLM-судья RAG-ответов. Оцени каждый кандидат от 1 до 5: "
        "фактическое соответствие reference, полнота ответа на вопрос, отсутствие фактов вне "
        "reference и наличие ссылок вида [1], [2] для фактических утверждений. Для вопроса "
        f"вне базы максимальный балл получает точный отказ «{UNKNOWN_ANSWER}». "
        "Верни ТОЛЬКО JSON-массив объектов candidate_id, score (целое 1..5), reason "
        "(одно короткое предложение). Не используй markdown.\n\n"
        + json.dumps(candidates, ensure_ascii=False)
    )
    response = await experiment_model(model_name=judge_model).ainvoke(prompt)
    judged = _parse_json_array(_message_text(response.content))
    by_key = {(str(row["implementation"]), str(row["question_id"])): row for row in records}
    for item in judged:
        candidate_id = str(item.get("candidate_id", ""))
        if candidate_id not in candidate_to_key:
            continue
        score = int(item["score"])
        if not 1 <= score <= 5:
            raise ValueError(f"Judge score out of range: {score}")
        row = by_key[candidate_to_key[candidate_id]]
        row["quality_score"] = score
        row["quality_reason"] = str(item.get("reason", "")).strip()
    if any(row.get("quality_score") is None for row in records):
        raise ValueError("Judge did not return a score for every candidate")
    RESULTS_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _usage_from_result(response: LLMResult) -> dict[str, int]:
    message_usage: dict[str, Any] = {}
    for generation_list in response.generations:
        for generation in generation_list:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if usage:
                message_usage = dict(usage)
                break
        if message_usage:
            break
    if message_usage:
        input_tokens = int(message_usage.get("input_tokens", 0) or 0)
        output_tokens = int(message_usage.get("output_tokens", 0) or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": int(
                message_usage.get("total_tokens", input_tokens + output_tokens) or 0
            ),
        }

    token_usage = (response.llm_output or {}).get("token_usage", {})
    input_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(token_usage.get("completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(token_usage.get("total_tokens", input_tokens + output_tokens) or 0),
    }


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end < start:
        raise ValueError(f"Judge returned no JSON array: {text[:300]}")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("Judge response must be an array")
    return data
