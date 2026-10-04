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
