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
