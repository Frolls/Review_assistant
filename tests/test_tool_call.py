import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings, default_ollama_base_url
from app.llm.client import ReviewAssistantClient
from app.prompts.loader import load_few_shot_examples, render_system_prompt
from app.tools.handlers import search_review_kb
from app.tools.schemas import SEARCH_REVIEW_KB_NAME, SEARCH_REVIEW_KB_PARAMETERS, validate_tool_arguments


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def make_response(content, tool_calls=None, total_tokens=0):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(total_tokens=total_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


def make_tool_call(tool_name, arguments, tool_call_id="call_1"):
    function = SimpleNamespace(name=tool_name, arguments=json.dumps(arguments, ensure_ascii=False))
    return SimpleNamespace(id=tool_call_id, function=function, type="function")


class ToolSchemaTests(unittest.TestCase):
    def test_validate_tool_arguments_accepts_valid_payload(self):
        payload = validate_tool_arguments(SEARCH_REVIEW_KB_NAME, {"query": "pep8 imports", "max_results": 2})
        self.assertEqual(payload, {"query": "pep8 imports", "max_results": 2})

    def test_validate_tool_arguments_rejects_extra_fields(self):
        with self.assertRaises(ValidationError):
            validate_tool_arguments(
                SEARCH_REVIEW_KB_NAME,
                {"query": "pep8 imports", "max_results": 2, "unexpected": "value"},
            )

    def test_tool_schema_is_generated_as_json_schema(self):
        self.assertEqual(SEARCH_REVIEW_KB_PARAMETERS["type"], "object")
        self.assertIn("query", SEARCH_REVIEW_KB_PARAMETERS["properties"])
        self.assertFalse(SEARCH_REVIEW_KB_PARAMETERS["additionalProperties"])

    def test_search_review_kb_reads_local_knowledge_base(self):
        result = search_review_kb("ansible idempotence", max_results=2)
        self.assertGreaterEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["source"], "Ansible Community Docs")


class SettingsTests(unittest.TestCase):
    def test_default_ollama_base_url_for_local_run(self):
        with patch("app.config.os.path.exists", return_value=False):
            self.assertEqual(default_ollama_base_url(), "http://localhost:11434/v1")

    def test_default_ollama_base_url_for_container_run(self):
        with patch("app.config.os.path.exists", return_value=True):
            self.assertEqual(default_ollama_base_url(), "http://host.docker.internal:11434/v1")


class PromptLoaderTests(unittest.TestCase):
    def test_load_few_shot_examples_from_separate_file(self):
        examples = load_few_shot_examples("v1")
        self.assertIn("Пример 1", examples)
        self.assertIn("search_review_kb", examples)

    def test_render_system_prompt_includes_few_shot_examples(self):
        prompt = render_system_prompt(product_name="PR Review Bot")
        self.assertIn("few-shot примеры", prompt)
        self.assertIn("Пример 2", prompt)


class ToolCallFlowTests(unittest.TestCase):
    def test_client_builds_openai_kwargs(self):
        settings = Settings(
            llm_provider="openai",
            openai_api_key="test-key",
            openai_base_url="https://api.openai.com/v1",
        )
        client = ReviewAssistantClient(client=FakeClient([]), settings=settings)
        self.assertEqual(
            client._build_client_kwargs(),
            {"api_key": "test-key", "base_url": "https://api.openai.com/v1"},
        )

    def test_client_builds_ollama_kwargs(self):
        settings = Settings(
            llm_provider="ollama",
            openai_model="qwen3",
            ollama_base_url="http://localhost:11434/v1",
        )
        client = ReviewAssistantClient(client=FakeClient([]), settings=settings)
        self.assertEqual(
            client._build_client_kwargs(),
            {"api_key": "ollama", "base_url": "http://localhost:11434/v1"},
        )

    def test_client_runs_full_tool_call_cycle(self):
        tool_call = make_tool_call(
            SEARCH_REVIEW_KB_NAME,
            {"query": "ansible idempotence", "max_results": 1},
        )
        fake_client = FakeClient(
            [
                make_response(content="", tool_calls=[tool_call], total_tokens=17),
                make_response(
                    content="Проверяйте idempotence и риск повторного запуска; это главный критерий для таких задач.",
                    total_tokens=11,
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "tool_call.log"
            settings = Settings(
                openai_api_key="test-key",
                log_path=str(log_path),
                product_name="PR Review Bot",
            )
            logger = logging.getLogger(f"test_logger_{id(self)}")
            logger.handlers = []
            logger.addHandler(logging.FileHandler(log_path, encoding="utf-8"))
            logger.setLevel(logging.INFO)
            logger.propagate = False

            client = ReviewAssistantClient(client=fake_client, settings=settings, logger=logger)
            run_result = client.run_with_details("Нужны правила ревью по Ansible idempotence.")
            answer = run_result.final_text

            self.assertIn("idempotence", answer.lower())
            self.assertEqual(len(fake_client.chat.completions.calls), 2)
            self.assertTrue(run_result.tool_used)
            self.assertEqual(run_result.tool_calls[0].tool_name, SEARCH_REVIEW_KB_NAME)
            self.assertEqual(run_result.tool_calls[0].arguments["max_results"], 1)
            self.assertEqual(run_result.usage_total_tokens, 28)

            second_request_messages = fake_client.chat.completions.calls[1]["messages"]
            self.assertEqual(second_request_messages[2]["role"], "assistant")
            self.assertIn("tool_calls", second_request_messages[2])
            self.assertEqual(second_request_messages[3]["role"], "tool")

            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn('"event": "tool_call"', log_text)
            self.assertIn('"event": "tool_result"', log_text)
            self.assertIn('"usage_total_tokens": 28', log_text)

    def test_client_returns_text_without_tool_call(self):
        fake_client = FakeClient(
            [make_response(content="Я помогаю с ревью PR и могу искать правила в базе знаний.", total_tokens=9)]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "no_tool.log"
            settings = Settings(openai_api_key="test-key", log_path=str(log_path))
            client = ReviewAssistantClient(client=fake_client, settings=settings)
            run_result = client.run_with_details("Привет, чем ты занимаешься?")
            answer = run_result.final_text

            self.assertIn("ревью", answer.lower())
            self.assertEqual(len(fake_client.chat.completions.calls), 1)
            self.assertFalse(run_result.tool_used)
            self.assertEqual(run_result.tool_calls, [])
            self.assertEqual(run_result.usage_total_tokens, 9)


if __name__ == "__main__":
    unittest.main()
