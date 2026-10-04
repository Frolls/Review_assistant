from __future__ import annotations

from types import SimpleNamespace
import json

import pytest

from app.core.config import Settings
from app.services.rag import CLARIFY_ANSWER, INSUFFICIENT_CONTEXT_ANSWER, UNKNOWN_ANSWER, RAGService


class FakeRetriever:
    def __init__(self, nodes: list[object]) -> None:
        self.nodes = nodes
        self.last_query: str | None = None
        self.calls = 0

    def retrieve(self, question: str) -> list[object]:
        self.calls += 1
        self.last_query = question
        return self.nodes


class FakeCompletions:
    def __init__(self, text: str) -> None:
        self.text = text
        self.assessment = '{"decision": "answer"}'
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=self.assessment if kwargs.get("response_format") else self.text
            ))]
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
@pytest.mark.parametrize("rerank", [False, True])
async def test_strong_hit_does_not_admit_weak_or_unscored_fragments(rerank):
    service = RAGService(settings(RAG_SCORE_THRESHOLD=0.5))
    good = scored_node(0.71)
    weak = scored_node(0.49)
    weak.node.text = "Unrelated class attributes."
    unscored = scored_node(None)
    unscored.node.text = "No retrieval confidence."
    service._retriever = FakeRetriever([good, weak, unscored])
    service._llm = FakeLLM("Use a dedicated module [1].")
    if rerank:
        def different_score_scale(question, nodes):
            assert nodes == [good]
            good.score = -2.0
            return nodes
        service._rerank_sync = different_score_scale

    prepared = await service.prepare("How should this task be written?")
    assert len(prepared.sources) == 1
    assert prepared.sources[0]["id"] == 1
    assert "Unrelated" not in prepared.context
    assert "No retrieval confidence" not in prepared.context
    assert prepared.top_score == 0.71


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
    prompt = fake_llm.chat.completions.calls[-1]["messages"][0]["content"]
    assert "[1] Файл: ansible.md" in prompt


@pytest.mark.asyncio
async def test_source_keeps_supporting_passage_at_end_of_chunk() -> None:
    service = RAGService(settings())
    node = scored_node(0.71)
    node.node.text = "Earlier section. " * 60 + "Mutable defaults are shared between calls. Use None."
    service._retriever = FakeRetriever([node])
    service._llm = FakeLLM("Use None [1].")

    result = await service.answer("How should a default list be declared?")

    assert result["sources"][0]["snippet"] == node.node.text


@pytest.mark.asyncio
async def test_missing_citation_is_disclosed_without_fabricated_marker() -> None:
    service = RAGService(settings())
    service._retriever = FakeRetriever([scored_node(0.71)])
    service._llm = FakeLLM("Use a dedicated module.")

    result = await service.answer("How should an Ansible task be written?")

    assert result["answer"] == "Use a dedicated module.\n\nОтвет не содержит ссылок на найденные источники."


@pytest.mark.asyncio
async def test_type_annotation_is_not_mistaken_for_source_marker() -> None:
    service = RAGService(settings())
    service._retriever = FakeRetriever([scored_node(0.71)])
    service._llm = FakeLLM("Return list[str].")

    result = await service.answer("What does the function return?")

    assert result["answer"] == "Return list[str].\n\nОтвет не содержит ссылок на найденные источники."


@pytest.mark.asyncio
async def test_evaluate_inputs_returns_full_context_after_one_retrieval() -> None:
    service = RAGService(settings())
    node = scored_node(0.71)
    node.node.text = "A" * 900
    retriever = FakeRetriever([node])
    service._retriever = retriever
    service._llm = FakeLLM("Grounded answer [1].")

    result = await service.evaluate_inputs("How should this task be reviewed?")

    assert retriever.calls == 1
    assert result["answer"] == "Grounded answer [1]."
    assert result["retrieved_contexts"] == ["A" * 900]
    assert result["latency_ms"] >= 0


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
    assert len(fake_llm.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_unknown_citation_id_is_rejected():
    service = RAGService(settings())
    service._retriever = FakeRetriever([scored_node(0.71)])
    service._llm = FakeLLM("A statement [999].")
    result = await service.answer("Question")
    assert result["answer"] == UNKNOWN_ANSWER


@pytest.mark.asyncio
async def test_array_index_in_code_is_not_a_citation():
    service = RAGService(settings())
    service._retriever = FakeRetriever([scored_node(0.71)])
    service._llm = FakeLLM("Use `items[0]` as in the source [1].")
    result = await service.answer("Question")
    assert "items[0]" in result["answer"]
    assert result["answer"] != UNKNOWN_ANSWER


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected", [
    ('{"decision":"clarify"}', CLARIFY_ANSWER),
    ('{"decision":"unknown"}', INSUFFICIENT_CONTEXT_ANSWER),
    ('{"decision":"unexpected"}', INSUFFICIENT_CONTEXT_ANSWER),
    ('not json', INSUFFICIENT_CONTEXT_ANSWER),
    ('[]', INSUFFICIENT_CONTEXT_ANSWER),
])
async def test_evidence_guard_skips_generation_and_sources(decision, expected):
    service = RAGService(settings())
    service._retriever = FakeRetriever([scored_node(0.95)])
    service._llm = FakeLLM("Must not generate a speculative answer [1].")
    service._llm.chat.completions.assessment = decision
    result = await service.answer("Why does this unspecified component fail?")
    assert result["answer"] == expected
    assert result["confident"] is False
    assert result["sources"] == []
    assert len(service._llm.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_evidence_guard_checks_original_question_without_assistant_claims():
    service = RAGService(settings(RAG_CONDENSE_ENABLED=True))
    service._retriever = FakeRetriever([scored_node(0.9)])
    service._llm = FakeLLM("Why is familiar code not idempotent?")
    service._llm.chat.completions.assessment = '{"decision":"clarify"}'
    prepared, stream = await service.stream_answer("Why is my unknown-component not idempotent?",
        history=[{"role":"user","content":"How do defaults work?"},
                 {"role":"assistant","content":"Unverified previous claim."}])
    assert ''.join([part async for part in stream]) == CLARIFY_ANSWER
    assert prepared.sources == []
    payload = json.loads(service._llm.chat.completions.calls[-1]["messages"][-1]["content"])
    assert payload["question"] == "Why is my unknown-component not idempotent?"
    assert payload["previous_questions"] == ["How do defaults work?"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434/v1", "none"),
    ("https://api.example.com/v1", None),
])
async def test_assessment_disables_ollama_reasoning_without_changing_external_provider(url, expected):
    service = RAGService(settings(OPENAI_BASE_URL=url))
    service._retriever = FakeRetriever([scored_node(0.9)])
    service._llm = FakeLLM("Answer [1].")
    await service.prepare("How should an Ansible task be written?")
    request = service._llm.chat.completions.calls[0]
    assert request.get("reasoning_effort") == expected
