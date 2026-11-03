import ast
import json

from langchain_core.messages import HumanMessage, ToolMessage
import pytest
import yaml

from app.core.config import Settings
from app.services.agent_persistent import _knowledge_answer
from app.services.code_review import review
from app.services.diagnostic_rules import retrieve
from app.services.rag import RAGService, finalize_answer
from app.services.review_checks import analyze
from app.services.review_contracts import diagnostics, evaluate, fix_passes
from tests.test_live_benchmark_regressions import reviewer
from tests.unit.test_rag_pipeline import FakeLLM, FakeRetriever, scored_node


@pytest.mark.asyncio
async def test_missing_import_preserves_docstring_future_and_behavior():
    code = '"""Config."""\nfrom __future__ import annotations\ndef decode(s):\n    return json.loads(s)\n'
    result = await analyze(code, "Самодостаточный модуль", "python")
    fix = result["diagnostics"][0]["suggested_fix"]
    assert not (await analyze(fix, "", "python"))["diagnostics"]
    # Execute only this inspected synthetic fixture, never production input.
    namespace = {}
    exec(fix, namespace)
    assert namespace["decode"]('{"enabled": true}') == {"enabled": True}
    assert ast.get_docstring(ast.parse(fix)) == "Config."
    unknown = await analyze("def f():\n    return project_helper()\n", "", "python")
    assert "suggested_fix" not in unknown["diagnostics"][0]


@pytest.mark.asyncio
async def test_finally_fix_preserves_cleanup_and_propagates_exception():
    code = "def reciprocal(value, events):\n    try:\n        return 1 / value\n    finally:\n        events.append('closed')\n        return 0\n"
    result = await analyze(code, "", "python")
    fix = result["diagnostics"][0]["suggested_fix"]
    namespace = {}
    exec(fix, namespace)
    events = []
    assert namespace["reciprocal"](4, events) == .25
    with pytest.raises(ZeroDivisionError):
        namespace["reciprocal"](0, events)
    assert events == ["closed", "closed"]
    side_effect = await analyze(code.replace("return 0", "return events.pop()"), "", "python")
    assert "suggested_fix" not in side_effect["diagnostics"][0]


def test_numeric_contract_detects_wrong_output_and_checks_repairs():
    task = "[2, 8] должно давать 5; [6, 9, 12] -> 9"
    before = "def average(values):\n    return sum(values) / (len(values) + 1)\n"
    fixed = before.replace("(len(values) + 1)", "len(values)")
    assert diagnostics(before, task)[0]["rule"] == "task-example"
    assert not diagnostics(fixed, task)
    assert fix_passes(before, fixed, task)
    assert not fix_passes(before, "def average(values):\n    return 5\n", task)
    assert not fix_passes(before, before, task)
    assert diagnostics("def double(x):\n    return x + 2\n", "3 -> 6; 5 -> 10")


@pytest.mark.parametrize("code", [
    "def f(x):\n    return __import__('os').system('echo forbidden')",
    "def f(x):\n    return x.__class__",
    "def f(sum):\n    return sum(sum)",
    "def f(x):\n    return x ** 1000000000",
    "def f(x):\n    return x * 'a'",
    "sum = lambda x: 4\ndef f(x):\n    return sum(x)",
    "@print\ndef f(x):\n    return x",
])
def test_contract_interpreter_rejects_effects_and_unbounded_operations(code):
    with pytest.raises((ValueError, TypeError)):
        evaluate(code, 3)
    assert diagnostics(code, "3 -> 6") == []


@pytest.mark.asyncio
async def test_exact_rule_survives_weak_vector_match_without_admitting_it():
    service = RAGService(Settings(_env_file=None, OPENAI_API_KEY="test", OPENAI_BASE_URL="https://example.test/v1",
                                  RAG_SCORE_THRESHOLD=.5, RAG_CONDENSE_ENABLED=False, RAG_RERANKER_ENABLED=False))
    await service._llm.close()
    service._retriever = FakeRetriever([scored_node(.12)])
    service._llm = FakeLLM("Python undefined name imports")
    prepared = await service.prepare("Проверь самодостаточный модуль", review_object="def decode(s):\n    return json.loads(s)\n")
    answer = finalize_answer(await review(service, prepared), prepared)
    assert service._retriever.calls == 1
    assert len(service._llm.chat.completions.calls) == 1
    assert answer.status == "answered" and "import json" in answer.text
    assert len(answer.cited_sources) == 1
    assert prepared.sources[0]["score"] is None
    assert prepared.sources[0]["file_name"].endswith("undefined-name/")
    assert retrieve({"diagnostics": [{"id": "0", "rule": "unknown"}]}) == []


@pytest.mark.asyncio
async def test_secret_inheritance_and_task_override():
    code = "- block:\n    - ansible.builtin.debug:\n        var: password\n  no_log: false\n"
    checks = await analyze(code, "password — секрет", "ansible")
    assert retrieve(checks)[0]["file_name"].startswith("https://docs.ansible.com/")
    fix = checks["diagnostics"][0]["suggested_fix"]
    assert not (await analyze(fix, "password — секрет", "ansible"))["diagnostics"]
    protected = code.replace("false", "true")
    assert not (await analyze(protected, "password — секрет", "ansible"))["diagnostics"]
    override = protected.replace("var: password", "var: password\n      no_log: false")
    assert (await analyze(override, "password — секрет", "ansible"))["diagnostics"]


@pytest.mark.asyncio
async def test_mkdir_fix_does_not_duplicate_module_command():
    checks = await analyze("- ansible.builtin.shell:\n    cmd: mkdir /tmp/example\n", "", "ansible")
    fixed = yaml.safe_load(checks["diagnostics"][0]["suggested_fix"])[0]
    assert fixed["args"] == {"creates": "/tmp/example"}
    assert fixed["ansible.builtin.shell"]["cmd"] == "mkdir /tmp/example"
    assert retrieve(checks)[0]["file_name"].endswith("shell_module.html")


@pytest.mark.asyncio
async def test_expression_repair_changes_only_return_and_preserves_contract():
    from app.services.rag import PreparedRAG
    code = "def average(values):\n    return sum(values) / (len(values) + 1)\n"
    task = "Арифметическое среднее: [2, 8] -> 5"
    checks = await analyze(code, task, "python")
    source = {**retrieve(checks)[0], "id": 1}
    checks["diagnostics"][0]["evidence_source_id"] = 1
    prepared = PreparedRAG(task, task, [source], None, True, "", [], review_object=code, review_checks=checks)
    service = reviewer([{"expression": "sum(values) / len(values)"}])
    answer = await review(service, prepared)
    assert "return sum(values) / len(values)" in answer and "```python" in answer
    assert "if len" not in answer


@pytest.mark.asyncio
async def test_single_item_example_is_data_not_an_unknown_citation():
    from app.services.rag import PreparedRAG
    code = "def twice_total(values):\n    return sum(values)\n"
    task = "[7] -> 14"
    checks = await analyze(code, task, "python")
    source = {**retrieve(checks)[0], "id": 1}
    checks["diagnostics"][0]["evidence_source_id"] = 1
    prepared = PreparedRAG(task, task, [source], None, True, "", [], review_object=code, review_checks=checks)
    service = reviewer([{"expression": "sum(values) * 2"}])
    answer = finalize_answer(await review(service, prepared), prepared)
    assert answer.status == "answered" and answer.confident
    assert "`[7]`" in answer.text and [s.id for s in answer.cited_sources] == [1]


@pytest.mark.asyncio
async def test_prepare_accounts_for_search_generation_usage():
    service = RAGService(Settings(_env_file=None, OPENAI_API_KEY="test", OPENAI_BASE_URL="https://example.test/v1",
                                  RAG_SCORE_THRESHOLD=.5, RAG_CONDENSE_ENABLED=False, RAG_RERANKER_ENABLED=False))
    await service._llm.close()
    service._retriever = FakeRetriever([scored_node(.12)])
    fake = reviewer([{"query": "Python undefined name"}])
    service._llm = fake._llm
    usages = []
    await service.prepare("Проверь", review_object="def f():\n    return json.loads('{}')\n", usage_callback=usages.append)
    assert usages == [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}]


def test_agent_publishes_exact_evidence_without_optional_invention():
    text = "A new list must be created when the default argument is None."
    message = ToolMessage(name="search_knowledge_base", tool_call_id="test", content=json.dumps(
        {"sources": [{"id": 1, "file_name": "rules.md", "text": text}]}))
    assert _knowledge_answer(message) == f"Найденное правило [1]:\n{text}\n\nИсточник: rules.md"
    assert _knowledge_answer(HumanMessage(content="Следующий вопрос")) is None
    for value in ({"sources": [3]}, {"sources": []}, {"sources": [{"id": "bad"}]}):
        message.content = json.dumps(value)
        assert "не вернул проверяемый источник" in _knowledge_answer(message)
