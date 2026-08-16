"""LangGraph supervisor used by the diploma agent layer."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.types import Command


RESEARCHER_PROMPT = """\
Ты researcher базы знаний по code review. Один раз вызови search_knowledge_base для
полного вопроса пользователя. Используй только результат tool и верни маркированный
список атомарных фактов с источниками [1], [2]. Финальный ответ не пиши.
Если confident=false, верни значение answer дословно.
"""

WRITER_PROMPT = """\
Ты writer. Получаешь вопрос и факты researcher. Составь краткий связный ответ только
из этих фактов, сохрани цитаты [1], [2] и не добавляй знания от себя. Если researcher
вернул отказ, верни его дословно.
"""


class SupervisorState(TypedDict):
    messages: Annotated[list[Any], add_messages]
    question: str
    research: str
    final_answer: str
    handoff_count: int


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def _final_text(result: dict[str, Any]) -> str:
    for message in reversed(result.get("messages", [])):
        if isinstance(message, AIMessage) and message.content:
            return _message_text(message.content).strip()
    return ""


def build_supervisor_graph(*, model: Any, search_tool: Any):
    """Build the checkpointable researcher → writer supervisor graph."""

    researcher = create_agent(
        model=model,
        tools=[search_tool],
        system_prompt=RESEARCHER_PROMPT,
        name="researcher",
    )
    writer = create_agent(
        model=model,
        tools=[],
        system_prompt=WRITER_PROMPT,
        name="writer",
    )

    async def supervisor(
        state: SupervisorState,
    ) -> Command[Literal["researcher", "writer", "__end__"]]:
        if not state.get("research"):
            return Command(goto="researcher", update={"handoff_count": state["handoff_count"] + 1})
        if not state.get("final_answer"):
            return Command(goto="writer", update={"handoff_count": state["handoff_count"] + 1})
        return Command(goto=END)

    async def researcher_node(state: SupervisorState, config: RunnableConfig):
        result = await researcher.ainvoke(
            {"messages": [{"role": "user", "content": state["question"]}]}, config=config
        )
        facts = _final_text(result)
        return Command(
            goto="supervisor",
            update={"research": facts, "messages": [AIMessage(content=facts, name="researcher")]},
        )

    async def writer_node(state: SupervisorState, config: RunnableConfig):
        task = f"Вопрос пользователя:\n{state['question']}\n\nФакты researcher:\n{state['research']}"
        result = await writer.ainvoke({"messages": [{"role": "user", "content": task}]}, config=config)
        answer = _final_text(result)
        return Command(
            goto="supervisor",
            update={"final_answer": answer, "messages": [AIMessage(content=answer, name="writer")]},
        )

    workflow = StateGraph(SupervisorState)
    workflow.add_node("supervisor", supervisor)
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("writer", writer_node)
    workflow.add_edge(START, "supervisor")
    return workflow.compile(checkpointer=InMemorySaver(), name="diploma_supervisor")
