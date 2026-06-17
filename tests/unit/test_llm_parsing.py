from types import SimpleNamespace

import pytest

from app.llm.parsing import extract_tool_calls, parse_json_object


def test_parse_json_object_accepts_markdown_json_fence():
    parsed = parse_json_object('```json\n{"score": 5, "ok": true}\n```')

    assert parsed == {"score": 5, "ok": True}


def test_parse_json_object_accepts_plain_json():
    parsed = parse_json_object('{"reasoning": "ok", "score": 4}')

    assert parsed["reasoning"] == "ok"
    assert parsed["score"] == 4


def test_parse_json_object_rejects_malformed_json():
    with pytest.raises(ValueError, match="valid JSON"):
        parse_json_object('{"score": }')


def test_extract_tool_calls_parses_function_arguments():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            type="function",
                            function=SimpleNamespace(
                                name="lookup_file",
                                arguments='{"path": "app/main.py", "limit": 10}',
                            ),
                        )
                    ]
                )
            )
        ]
    )

    calls = extract_tool_calls(response)

    assert calls == [
        {
            "id": "call_1",
            "type": "function",
            "name": "lookup_file",
            "arguments": {"path": "app/main.py", "limit": 10},
        }
    ]
