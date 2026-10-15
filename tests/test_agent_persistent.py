from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.services.agent_persistent import build_agent


class TelegramRequestModel:
    """Deterministic model: request one guarded action, then finish."""

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="Сценарий завершён.")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "send_telegram_message",
                    "args": {"chat_id": 1001, "text": "PR #42 готов к review"},
                    "id": "request-pr-42",
                }
            ],
        )


def initial_state() -> dict:
    return {"messages": [HumanMessage(content="Отправь статус PR #42")]}


def config(thread_id: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
            "user_role": "write-with-approve",
        }
    }


@pytest.mark.asyncio
async def test_graph_pauses_before_dangerous_tool() -> None:
    sender = AsyncMock()
    async with AsyncSqliteSaver.from_conn_string(":memory:") as checkpointer:
        await checkpointer.setup()
        graph = build_agent(checkpointer, model=TelegramRequestModel(), sender=sender)

        result = await graph.ainvoke(initial_state(), config("interrupt-case"))
        snapshot = await graph.aget_state(config("interrupt-case"))

    assert result["__interrupt__"][0].value["type"] == "approve_telegram_message"
    assert snapshot.next == ("confirm_and_execute_telegram_message",)
    assert snapshot.values["pending_action"]["text"] == "PR #42 готов к review"
    assert snapshot.values["sent"] is False
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_approval_executes_side_effect_and_marks_sent() -> None:
    sender = AsyncMock()
    async with AsyncSqliteSaver.from_conn_string(":memory:") as checkpointer:
        await checkpointer.setup()
        graph = build_agent(checkpointer, model=TelegramRequestModel(), sender=sender)
        run_config = config("approve-case")

        await graph.ainvoke(initial_state(), run_config)
        result = await graph.ainvoke(Command(resume=True), run_config)

    assert result["sent"] is True
    assert result["decision"] is True
    sender.assert_awaited_once_with(1001, "PR #42 готов к review")


@pytest.mark.asyncio
async def test_rejection_does_not_execute_side_effect() -> None:
    sender = AsyncMock()
    async with AsyncSqliteSaver.from_conn_string(":memory:") as checkpointer:
        await checkpointer.setup()
        graph = build_agent(checkpointer, model=TelegramRequestModel(), sender=sender)
        run_config = config("reject-case")

        await graph.ainvoke(initial_state(), run_config)
        result = await graph.ainvoke(Command(resume=False), run_config)

    assert result["sent"] is False
    assert result["decision"] is False
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_iteration_budget_resets_for_every_user_turn():
    class TimeModel:
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages):
            if isinstance(messages[-1], HumanMessage):
                return AIMessage(content="", tool_calls=[{"name": "get_current_time", "args": {}, "id": "clock"}])
            return AIMessage(content="Done")
    async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
        await saver.setup()
        graph = build_agent(saver, model=TimeModel(), sender=AsyncMock())
        for turn in range(5):
            result = await graph.ainvoke({"messages": [HumanMessage(content=f"Time {turn}")]}, config("repeat"))
            assert result["iteration_count"] == 2
            assert result["tool_results"][-1]["name"] == "get_current_time"


@pytest.mark.asyncio
async def test_graph_rejects_another_recipient_even_before_approval():
    sender = AsyncMock()
    async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
        await saver.setup()
        graph = build_agent(saver, model=TelegramRequestModel(), sender=sender)
        cfg = config("recipient")
        cfg["configurable"]["allowed_recipient"] = "2002"
        with pytest.raises(ValueError, match="инициатора"):
            await graph.ainvoke(initial_state(), cfg)
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_delivery_decision_finishes_without_another_model_call():
    model = TelegramRequestModel()
    model.ainvoke = AsyncMock(wraps=model.ainvoke)
    async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
        await saver.setup()
        graph = build_agent(saver, model=model, sender=AsyncMock())
        cfg = config("deterministic-cancel")
        await graph.ainvoke(initial_state(), cfg)
        result = await graph.ainvoke(Command(resume=False), cfg)
        assert not (await graph.aget_state(cfg)).next
    assert result["sent"] is False
    assert "отменена" in result["messages"][-1].content
    assert model.ainvoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_prefix", [False, True])
async def test_time_request_cannot_prepare_delivery_even_after_a_previous_send(legacy_prefix):
    class ConfusedModel:
        def __init__(self):
            self.tools = []

        def bind_tools(self, tools):
            self.tools = [tool.name for tool in tools]
            return self

        async def ainvoke(self, messages):
            latest = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
            if latest == "Отправь мне сообщение: тест":
                assert "send_telegram_message" in self.tools
                return AIMessage(content="", tool_calls=[{
                    "name": "send_telegram_message", "args": {"chat_id": 1001, "text": "тест"}, "id": "explicit-send",
                }])
            assert "send_telegram_message" not in self.tools
            if isinstance(messages[-1], HumanMessage):
                # Simulate a model hallucinating a tool that was not offered.
                return AIMessage(content="", tool_calls=[{
                    "name": "send_telegram_message", "args": {"chat_id": 1001, "text": "Old time"}, "id": "unwanted-send",
                }])
            if isinstance(messages[-1], ToolMessage) and messages[-1].name == "send_telegram_message":
                return AIMessage(content="", tool_calls=[{
                    "name": "get_current_time", "args": {"timezone": "Asia/Yekaterinburg"}, "id": "fresh-time",
                }])
            return AIMessage(content=messages[-1].content)

    sender = AsyncMock()
    async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
        await saver.setup()
        graph = build_agent(saver, model=ConfusedModel(), sender=sender)
        cfg = config("send-then-time")
        await graph.ainvoke({"messages": [HumanMessage(content="Отправь мне сообщение: тест")]}, cfg)
        await graph.ainvoke(Command(resume=False), cfg)
        question = "Который сейчас час в Asia/Yekaterinburg? Сообщи текущее время."
        if legacy_prefix:
            question = f"Telegram ID инициатора: 1001. Запрос пользователя: {question}"
        for _ in range(2):
            result = await graph.ainvoke({"messages": [HumanMessage(content=question)]}, cfg)
            assert result["pending_action"] is None
            assert not (await graph.aget_state(cfg)).next
            assert result["tool_results"][-1]["name"] == "get_current_time"
            assert result["tool_results"][-1]["error"] is None
            assert result["messages"][-1].content.endswith("+05:00")
    sender.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("obeys_tool_choice", [True, False])
async def test_explicit_time_command_requires_new_tool_result_in_every_turn(obeys_tool_choice):
    class Model:
        def bind_tools(self, tools, **kwargs):
            self.required = kwargs.get("tool_choice")
            return self

        async def ainvoke(self, messages):
            latest = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
            if latest.startswith("Вызови get_current_time"):
                assert all(m.content != "Старое время: 22:34:57" for m in messages)
            if self.required and obeys_tool_choice:
                return AIMessage(content="", tool_calls=[{
                    "name": self.required, "args": {"timezone": "Asia/Yekaterinburg"}, "id": "new-clock",
                }])
            if isinstance(messages[-1], ToolMessage):
                return AIMessage(content=messages[-1].content)
            return AIMessage(content="Старое время: 22:34:57")

    async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
        await saver.setup()
        graph = build_agent(saver, model=Model(), sender=AsyncMock())
        cfg = config("force-fresh-time")
        await graph.ainvoke({"messages": [HumanMessage(content="Ранее полученное время")]}, cfg)
        request = {"messages": [HumanMessage(content="Вызови get_current_time для Asia/Yekaterinburg и сообщи текущее время.")]}
        if not obeys_tool_choice:
            with pytest.raises(RuntimeError, match="свежих данных"):
                await graph.ainvoke(request, cfg)
            return
        results = []
        for _ in range(2):
            # Create a new message, as a real API request does.
            result = await graph.ainvoke({"messages": [HumanMessage(content=request["messages"][0].content)]}, cfg)
            assert result["tool_results"][-1]["name"] == "get_current_time"
            assert result["messages"][-1].content == result["tool_results"][-1]["result"]
            results.append(result["tool_results"][-1]["result"])
        assert results[0] != results[1]
