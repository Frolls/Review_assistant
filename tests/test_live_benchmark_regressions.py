import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.chat import service as chat_service
from app.core.config import Settings
from app.services.code_review import review
from app.services.model_adapter import ModelAdapter
from app.services.ollama import configure_context, model_options
from app.services.rag import PreparedRAG, UNKNOWN_ANSWER, finalize_answer


def test_missing_or_corrupt_tokenizer_cache_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    def network_forbidden(*args):
        raise AssertionError("request attempted to initialize a network tokenizer")
    monkeypatch.setattr(chat_service.tiktoken, "get_encoding", network_forbidden)
    try:
        for content in (None, b"broken"):
            if content:
                (tmp_path / "fb374d419588a4632f3f557e76b4b70aebbca790").write_bytes(content)
            chat_service._token_encoder.cache_clear()
            assert chat_service._count_text_tokens("код") == len("код".encode())
    finally:
        chat_service._token_encoder.cache_clear()


@pytest.mark.asyncio
async def test_context_configuration_is_derived_verified_and_idempotent():
    settings = Settings(_env_file=None, OPENAI_API_KEY="test", OPENAI_BASE_URL="http://localhost:11434/v1",
                        DEFAULT_MODEL="qwen3:latest", VISION_MODEL=None, REVIEW_MODEL=None, LLM_NUM_CTX=8192)
    models = {"qwen3:latest": {"name": "qwen3:latest", "digest": "base", "context": 4096}}
    created = []
    def handle(request):
        data = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": list(models.values())})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"parameters": f"num_ctx {models[data['model']]['context']}\n"})
        assert request.url.path == "/api/create"
        created.append(data)
        models[data["model"]] = {"name": data["model"], "digest": "derived", "context": data["parameters"]["num_ctx"]}
        return httpx.Response(200, json={"status": "success"})
    transport = httpx.MockTransport(handle)
    await configure_context(settings, transport=transport)
    assert settings.default_model.startswith("review-context-")
    assert created[0]["from"] == "qwen3:latest"
    assert models["qwen3:latest"]["context"] == 4096
    await configure_context(settings, transport=transport)
    assert len(created) == 1


@pytest.mark.asyncio
async def test_context_configuration_rejects_unapplied_parameter():
    settings = SimpleNamespace(openai_base_url="http://ollama:11434/v1", llm_num_ctx=8192,
                               default_model="qwen3:latest", vision_model=None, request_timeout=1)
    def handle(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:latest", "digest": "base"}]})
        if request.url.path == "/api/create":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(200, json={"parameters": "num_ctx 4096"})
    with pytest.raises(RuntimeError, match="context does not match"):
        await configure_context(settings, transport=httpx.MockTransport(handle))


def test_only_ollama_gets_supported_reasoning_parameter():
    assert model_options("http://localhost:11434/v1") == {"reasoning_effort": "none"}
    assert model_options("http://ollama:8080/v1") == {"reasoning_effort": "none"}
    assert model_options("https://api.openai.com/v1") == {}


def prepared_review():
    rule = "When documentation exists, it must match the signature."
    source = {"id": 1, "file_name": "rules.md", "page": 1, "score": .8, "snippet": rule}
    return PreparedRAG("Review this code", "function", [source], .8, True, rule, [rule],
                       review_object="def identity(value):\n    return value\n")


def reviewer(payloads):
    responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(p)))],
                                 usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
                 for p in payloads]
    create = AsyncMock(side_effect=responses)
    return SimpleNamespace(settings=SimpleNamespace(llm_num_ctx=8192, default_model="test",
                                                    openai_base_url="http://ollama:11434/v1"),
                           adapter=ModelAdapter(1), _llm=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))


@pytest.mark.asyncio
async def test_clean_review_publishes_scoped_answer_with_checked_rules():
    prepared = prepared_review()
    service = reviewer([{"checked_source_ids": [1], "findings": []}])
    answer = finalize_answer(await review(service, prepared), prepared)
    assert answer.status == "answered" and answer.confident
    assert "не обнаружено" in answer.text and "[1]" in answer.text
    assert len(answer.cited_sources) == 1


def docstring_finding():
    return {"code_excerpt": "def identity(value):", "problem": "Missing docstring",
            "source_id": 1, "source_quote": "When documentation exists, it must match the signature.",
            "fix": "", "language": "python"}


@pytest.mark.asyncio
async def test_conditional_rule_rejection_removes_finding_and_meters_both_calls():
    prepared = prepared_review()
    service = reviewer([{"checked_source_ids": [1], "findings": [docstring_finding()]},
                        {"decisions": [{"index": 0, "supported": False, "fix_valid": False}]}])
    usage = []
    answer = await review(service, prepared, usage_callback=usage.append)
    assert "Missing docstring" not in answer and "не обнаружено" in answer
    assert len(usage) == 2
    verification_input = json.loads(service._llm.chat.completions.create.call_args.kwargs["messages"][1]["content"])
    assert verification_input["code"] == prepared.review_object
    assert verification_input["rules"][0]["text"] == prepared.sources[0]["snippet"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"source_id": 99}, {"source_quote": "Invented rule"}, {"code_excerpt": "invented code"}])
async def test_unverifiable_finding_is_refused_with_no_published_sources(change):
    prepared = prepared_review()
    service = reviewer([{"checked_source_ids": [1], "findings": [{**docstring_finding(), **change}]}])
    answer = finalize_answer(await review(service, prepared), prepared)
    assert answer.text == UNKNOWN_ANSWER and answer.status == "refused"
    assert not answer.cited_sources and not answer.confident


@pytest.mark.asyncio
async def test_slow_tokenizer_work_does_not_block_other_tasks(monkeypatch):
    import time
    from app.chat.domain import Chat
    from tests.chat.test_service_context import FakeLLM, FakeRepository
    real_fit = chat_service.fit_to_budget
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    def slow_fit(*args):
        loop.call_soon_threadsafe(entered.set)
        time.sleep(.15)
        return real_fit(*args)
    monkeypatch.setattr(chat_service, "fit_to_budget", slow_fit)
    chat = Chat(owner_external_id="test", interface="eval")
    service = chat_service.ChatService(FakeRepository(chat), FakeLLM())
    async def consume():
        return [part async for part in service.send_message(chat.id, "hello", mode="transform")]
    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.sleep(.01)
    assert not task.done()
    assert await task == ["Hello, ", "Anya"]


@pytest.mark.asyncio
async def test_review_without_rag_never_falls_back_to_ungrounded_model():
    from fastapi import HTTPException
    from tests.chat.test_service_context import FakeLLM
    llm = FakeLLM()
    service = chat_service.ChatService(None, llm)
    with pytest.raises(HTTPException) as error:
        _ = [part async for part in service.send_message("missing", "review")]
    assert error.value.status_code == 503
    assert not llm.chat.completions.calls


@pytest.mark.asyncio
async def test_invalid_python_fix_is_not_published_even_when_model_approves_it():
    prepared = prepared_review()
    finding = {**docstring_finding(), "fix": "return value\n    broken"}
    service = reviewer([{"checked_source_ids": [1], "findings": [finding]},
                        {"decisions": [{"index": 0, "supported": True, "fix_valid": True}]}])
    answer = await review(service, prepared)
    assert "Missing docstring" in answer
    assert "```" not in answer and "broken" not in answer


def test_quote_normalization_preserves_words_and_negation():
    from app.services.code_review import _quote_text
    assert _quote_text("Use `None`\n here") == _quote_text("Use None here")
    assert _quote_text("Do not use `None`") != _quote_text("Do use None")


@pytest.mark.asyncio
@pytest.mark.parametrize("code, expected", [
    ("def f(x, items=[]):\n    items.append(x)\n    return items\n", {"B006"}),
    ("def f(x, items=None):\n    if items is None:\n        items = []\n    items.append(x)\n    return items\n", set()),
    ("def f(path):\n    return open(path).read()\n", {"OPEN_READ"}),
    ("def f(path):\n    with open(path) as stream:\n        return stream.read()\n", set()),
])
async def test_real_python_checks_separate_defects_and_corrections(code, expected):
    from app.services.review_checks import analyze
    result = await analyze(code, "review", "python")
    assert {d["rule"] for d in result["diagnostics"]} == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("code, task, expected", [
    ("- shell: mkdir /tmp/example\n", "review", {"mkdir-repeat"}),
    ("- shell: mkdir -p /tmp/example\n", "review", set()),
    ("- shell: mkdir /tmp/example\n  args:\n    creates: /tmp/example\n", "review", set()),
    ("- debug:\n    var: access_key\n", "access_key — секрет", {"secret-debug"}),
    ("- debug:\n    var: access_key\n  no_log: true\n", "access_key — секрет", set()),
    ("- no_log: true\n  block:\n    - debug:\n        var: access_key\n", "access_key — секрет", set()),
    ("- &cycle\n  block: [*cycle]\n", "review", set()),
    ("- ansible.builtin.package:\n    name: git\n    state: present\n", "review", set()),
])
async def test_ansible_checks_respect_guards_and_secret_requirement(code, task, expected):
    from app.services.review_checks import analyze
    result = await analyze(code, task, "ansible")
    assert {d["rule"] for d in result["diagnostics"]} == expected


@pytest.mark.asyncio
async def test_no_source_clean_check_is_scoped_and_not_confident():
    from app.services.review_checks import analyze
    prepared = prepared_review()
    prepared.sources = []
    prepared.confident = False
    prepared.review_checks = await analyze(prepared.review_object, "review", "python")
    service = reviewer([])
    result = finalize_answer(await review(service, prepared), prepared)
    assert result.status == "answered" and not result.confident and not result.cited_sources
    assert "не полное доказательство" in result.text and "B006" in result.text
    service._llm.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
async def test_clean_static_review_does_not_publish_retrieval_candidates():
    from app.services.review_checks import analyze
    prepared = prepared_review()
    prepared.review_checks = await analyze(prepared.review_object, "review", "python")
    result = finalize_answer(await review(reviewer([]), prepared), prepared)
    assert result.status == "answered" and not result.cited_sources and not result.confident
    assert "[1]" not in result.text


def test_complete_fix_preserves_interface_and_requires_real_yaml_structure():
    from app.services.code_review import complete_fix
    original = 'import subprocess\ndef size(path):\n    return subprocess.run("wc " + path, shell=True).stdout\n'
    assert not complete_fix(original, 'import os\ndef size(path):\n    return os.path.getsize(path)\n', "python")
    assert not complete_fix(original, 'import subprocess\nsubprocess.run(["wc"])\n', "python")
    assert complete_fix(original, 'import subprocess\ndef size(path):\n    return subprocess.run(["wc", path]).stdout\n', "python")
    assert not complete_fix("- shell: mkdir /tmp/a", "mkdir -p /tmp/a", "ansible")


@pytest.mark.asyncio
async def test_mechanical_mkdir_fix_preserves_task_and_passes_checks():
    import yaml
    from app.services.review_checks import analyze
    code = "- name: Create cache\n  shell: mkdir /tmp/cache\n  args:\n    executable: /bin/sh\n"
    analysis = await analyze(code, "review", "ansible")
    fix = analysis["diagnostics"][0]["suggested_fix"]
    task = yaml.safe_load(fix)[0]
    assert task["shell"] == "mkdir /tmp/cache"
    assert task["args"] == {"executable": "/bin/sh", "creates": "/tmp/cache"}
    assert not (await analyze(fix, "review", "ansible"))["diagnostics"]


@pytest.mark.asyncio
async def test_mechanical_exception_fix_preserves_fallback_and_propagates_interrupts():
    from app.services.review_checks import analyze
    code = "def number(value):\n    try:\n        return int(value)\n    except:\n        return 0\n"
    analysis = await analyze(code, "review", "python")
    fix = next(d["suggested_fix"] for d in analysis["diagnostics"] if d["rule"] == "E722")
    namespace = {}
    exec(fix, namespace)
    assert namespace["number"](None) == 0
    assert namespace["number"]("bad") == 0
    assert namespace["number"]("7") == 7
    class Interrupted:
        def __int__(self):
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        namespace["number"](Interrupted())


@pytest.mark.asyncio
async def test_diagnostic_ids_are_constrained_in_provider_schema():
    prepared = prepared_review()
    prepared.review_checks = {"language": "python", "scope": "test", "diagnostics": [
        {"id": "actual-id", "rule": "test", "message": "test", "line": 1}]}
    service = reviewer([{"checked_source_ids": [1], "findings": []}])
    assert await review(service, prepared) == UNKNOWN_ANSWER
    schema = service._llm.chat.completions.create.call_args.kwargs["response_format"]["json_schema"]["schema"]
    finding = schema["$defs"]["Finding"]
    assert finding["properties"]["diagnostic_id"]["enum"] == ["actual-id"]
    assert "diagnostic_id" in finding["required"]


@pytest.mark.asyncio
async def test_mechanical_shell_fix_preserves_return_and_passes_literal_argument():
    from unittest.mock import patch
    from app.services.review_checks import analyze
    code = 'import subprocess\ndef size(имя):\n    return subprocess.run("wc -c -- " + имя, shell=True, check=True, capture_output=True, text=True).stdout\n'
    result = await analyze(code, "review", "python")
    fix = next(d["suggested_fix"] for d in result["diagnostics"] if d["rule"] == "S602")
    namespace = {}
    exec(fix, namespace)
    with patch("subprocess.run", return_value=SimpleNamespace(stdout="expected")) as run:
        assert namespace["size"]("file name; echo unsafe") == "expected"
        assert run.call_args.args == (["wc", "-c", "--", "file name; echo unsafe"],)
        assert run.call_args.kwargs == {"shell": False, "check": True, "capture_output": True, "text": True}
    assert not (await analyze(fix, "review", "python"))["diagnostics"]


@pytest.mark.asyncio
async def test_complex_shell_command_gets_no_mechanical_fix():
    from app.services.review_checks import analyze
    code = 'import subprocess\ndef run(x):\n    return subprocess.run("cat -- " + x + " | sort", shell=True)\n'
    result = await analyze(code, "review", "python")
    assert result["diagnostics"] and all("suggested_fix" not in d for d in result["diagnostics"])


def test_directory_layout_cannot_ground_shell_idempotency():
    from app.services.review_checks import source_matches_diagnostic
    diagnostic = {"rule": "mkdir-repeat"}
    assert not source_matches_diagnostic(diagnostic, "Структура role должна использовать каталоги main.yml; directories можно не создавать.")
    assert not source_matches_diagnostic(diagnostic, "Повторное использование roles.")
    assert source_matches_diagnostic(diagnostic, "shell и command требуют проверки идемпотентности; creates предотвращает повторный запуск.")
