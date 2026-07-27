from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.rag import UNKNOWN_ANSWER, RAGService


class FakeRetriever:
    def __init__(self, nodes: list[object]) -> None:
        self.nodes = nodes
        self.last_query: str | None = None

    def retrieve(self, question: str) -> list[object]:
        self.last_query = question
        return self.nodes


class FakeCompletions:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))]
        )


class FakeLLM:
    def __init__(self, text: str) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(text))

    async def close(self) -> None:
        return None


def settings(**overrides) -> Settings:
    values = {
        "OPENAI_API_KEY": "test",
        "RAG_SCORE_THRESHOLD": 0.3,
        "RAG_CONDENSE_ENABLED": False,
        "RAG_RERANKER_ENABLED": False,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def scored_node(score: float) -> object:
    node = SimpleNamespace(
        id_="node-1",
        text="Use specialized Ansible modules for idempotency.",
        metadata={"file_name": "ansible.md", "page": 2},
    )
    node.get_content = lambda: node.text
    return SimpleNamespace(node=node, score=score)


@pytest.mark.asyncio
async def test_score_guard_skips_answer_llm_call() -> None:
    service = RAGService(settings())
    service._retriever = FakeRetriever([scored_node(0.12)])
    fake_llm = FakeLLM("must not be used")
    service._llm = fake_llm

    result = await service.answer("When should I plant tomatoes?")

    assert result["answer"] == UNKNOWN_ANSWER
    assert result["confident"] is False
    assert result["sources"] == []
    assert fake_llm.chat.completions.calls == []


@pytest.mark.asyncio
async def test_confident_answer_has_numbered_structured_source() -> None:
    service = RAGService(settings())
    service._retriever = FakeRetriever([scored_node(0.71)])
    fake_llm = FakeLLM("Use a dedicated module [1].")
    service._llm = fake_llm

    result = await service.answer("How should an Ansible task be written?")

    assert result["confident"] is True
    assert result["sources"][0] == {
        "id": 1,
        "file_name": "ansible.md",
        "page": 2,
        "score": 0.71,
        "snippet": "Use specialized Ansible modules for idempotency.",
    }
    prompt = fake_llm.chat.completions.calls[0]["messages"][0]["content"]
    assert "[1] Файл: ansible.md" in prompt


@pytest.mark.asyncio
async def test_condense_rewrites_followup_only_for_retrieval() -> None:
    service = RAGService(settings(RAG_CONDENSE_ENABLED=True))
    retriever = FakeRetriever([scored_node(0.71)])
    service._retriever = retriever
    fake_llm = FakeLLM(
        "How can command and shell be made idempotent in Ansible?"
    )
    service._llm = fake_llm

    prepared = await service.prepare(
        "And how for them?",
        history=[
            {
                "role": "user",
                "content": "Why should command and shell be avoided in Ansible?",
            },
            {
                "role": "assistant",
                "content": "Specialized modules are declarative and idempotent.",
            },
        ],
        chat_id="chat-1",
    )

    assert prepared.original_question == "And how for them?"
    assert prepared.retrieval_question.startswith(
        "How can command and shell be made idempotent in Ansible?"
    )
    assert (
        "Why should command and shell be avoided in Ansible?"
        in prepared.retrieval_question
    )
    assert retriever.last_query == prepared.retrieval_question
    assert len(fake_llm.chat.completions.calls) == 1
