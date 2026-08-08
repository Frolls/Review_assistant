from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services import agent_graph


def state_with(message: Any, iteration_count: int = 1) -> agent_graph.AgentState:
    return {
        "messages": [message],
        "iteration_count": iteration_count,
        "tool_results": [],
    }


def test_router_has_explicit_terminal_branches() -> None:
    final = state_with(AIMessage(content="готово"))
    tool_call = state_with(
        AIMessage(
            content="",
            tool_calls=[{"name": "missing", "args": {}, "id": "call-1"}],
        )
    )

    assert agent_graph.route_after_model(final) == "force_finish"
    assert agent_graph.route_after_model(tool_call) == "execute_tool"
    tool_call["iteration_count"] = agent_graph.MAX_ITERATIONS
    assert agent_graph.route_after_model(tool_call) == "force_finish"


@pytest.mark.asyncio
async def test_unknown_tool_becomes_serializable_observation() -> None:
    current = state_with(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "get_user_balance", "args": {"user": 1}, "id": "call-x"}
            ],
        )
    )

    update = await agent_graph.execute_tool(current)

    assert isinstance(update["messages"][0], ToolMessage)
    assert update["messages"][0].tool_call_id == "call-x"
    assert "unknown tool" in update["messages"][0].content
    assert update["tool_results"] == [
        {
            "name": "get_user_balance",
            "args": {"user": 1},
            "result": "error: unknown tool 'get_user_balance'",
            "error": "unknown tool 'get_user_balance'",
        }
    ]


@pytest.mark.asyncio
async def test_force_finish_adds_explicit_answer_at_limit() -> None:
    current = state_with(
        AIMessage(
            content="",
            tool_calls=[{"name": "missing", "args": {}, "id": "call-6"}],
        ),
        iteration_count=agent_graph.MAX_ITERATIONS,
    )

    update = await agent_graph.force_finish(current)

    assert "достигнут лимит" in update["messages"][0].content


@pytest.mark.asyncio
async def test_custom_graph_stops_repeated_unknown_tool(monkeypatch) -> None:
    class RepeatingModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            index = sum(isinstance(message, ToolMessage) for message in messages) + 1
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "always_missing", "args": {}, "id": f"call-{index}"}
                ],
            )

    monkeypatch.setattr(agent_graph, "model", RepeatingModel())
    result = await agent_graph.custom_graph.ainvoke(
        {
            "messages": [HumanMessage(content="Повторяй сломанный tool")],
            "iteration_count": 0,
            "tool_results": [],
        }
    )

    assert result["iteration_count"] == agent_graph.MAX_ITERATIONS
    assert len(result["tool_results"]) == agent_graph.MAX_ITERATIONS - 1
    assert "достигнут лимит" in result["messages"][-1].content
