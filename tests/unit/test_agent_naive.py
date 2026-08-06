from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.services import agent_naive


def completion(*, content=None, tool_calls=None, prompt_tokens=10, output_tokens=3):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls)
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
        ),
    )


def tool_call(name: str, arguments: str = "{}"):
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        captured = dict(kwargs)
        captured["messages"] = list(kwargs["messages"])
        self.calls.append(captured)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def install_client(monkeypatch, responses):
    completions = FakeCompletions(responses)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(agent_naive, "OpenAI", lambda **kwargs: fake)
    return completions


def test_agent_dispatches_tool_and_returns_trace(monkeypatch) -> None:
    calls = install_client(
        monkeypatch,
        [
            completion(tool_calls=[tool_call("get_current_time")]),
            completion(content="Готово", prompt_tokens=20, output_tokens=2),
        ],
    )
    monkeypatch.setitem(agent_naive.DISPATCH, "get_current_time", lambda: "now")

    result = agent_naive.run_agent("Который час?")

    assert result["answer"] == "Готово"
    assert result["steps"] == 2
    assert result["trace"][0]["tool_result"] == "now"
    assert result["trace"][1]["llm_input_tokens"] == 20
    assert calls.calls[0]["model"] == agent_naive.get_settings().default_model
    assert calls.calls[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "now",
    }


def test_unknown_tool_is_returned_to_model(monkeypatch) -> None:
    calls = install_client(
        monkeypatch,
        [
            completion(tool_calls=[tool_call("get_user_balance")]),
            completion(content="Такого инструмента нет."),
        ],
    )

    result = agent_naive.run_agent("Проверь баланс")

    assert result["answer"] == "Такого инструмента нет."
    assert "не разрешён" in result["trace"][0]["tool_result"]
    assert calls.calls[1]["messages"][-1]["role"] == "tool"


def test_provider_error_has_stable_result(monkeypatch) -> None:
    install_client(monkeypatch, [RuntimeError("offline")])

    result = agent_naive.run_agent("Задача")

    assert result["error"] == "offline"
    assert result["steps"] == 1
    assert result["trace"][0]["duration_ms"] >= 0


def test_tools_have_two_sentence_descriptions() -> None:
    assert set(agent_naive.DISPATCH) == {
        "search_knowledge_base",
        "get_current_time",
        "send_telegram_message",
    }
    for tool in agent_naive.TOOLS:
        assert tool["function"]["description"].count(".") >= 2


def test_simple_tools_are_local(capsys) -> None:
    current = datetime.fromisoformat(agent_naive.get_current_time("Asia/Yekaterinburg"))
    assert current.utcoffset() is not None
    assert agent_naive.send_telegram_message("42", "test") == "Сообщение отправлено в 42"
    assert capsys.readouterr().out == "[TELEGRAM → 42] test\n"
