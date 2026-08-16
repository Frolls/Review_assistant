from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.types import Command

from experiments.common import (
    ROOT,
    TEST_QUESTIONS,
    UsageTracker,
    close_rag_service,
    experiment_model,
    final_ai_text,
    judge_results_if_complete,
    make_result,
    save_results,
    search_knowledge_base,
)


RESEARCHER_PROMPT = """\
Ты researcher корпоративной базы знаний по code review.
Для каждого задания ровно один раз вызови search_knowledge_base, передав полный вопрос.
Используй только tool result. Если confident=true, верни маркированный список атомарных
фактов, и у каждого факта укажи источник [1], [2]. Не пиши финальный ответ пользователю.
Если confident=false, верни дословно значение поля answer и больше ничего.
"""

WRITER_PROMPT = """\
Ты writer. Получаешь вопрос пользователя и уже найденные researcher факты.
Собери краткий связный финальный ответ только из этих фактов. Сохрани ссылки [1], [2]
у подтверждаемых ими утверждений. Не добавляй знания от себя и не имитируй поиск.
Если researcher вернул отказ «по базе не нашёл, могу эскалировать», верни его дословно.
"""


class SupervisorState(TypedDict):
    messages: Annotated[list[Any], add_messages]
    question: str
    research: str
    final_answer: str
    handoff_count: int


def build_app():
    model = experiment_model()
    researcher = create_agent(
        model=model,
        tools=[search_knowledge_base],
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
            return Command(
                goto="researcher",
                update={"handoff_count": state.get("handoff_count", 0) + 1},
            )
        if not state.get("final_answer"):
            return Command(
                goto="writer",
                update={"handoff_count": state.get("handoff_count", 0) + 1},
            )
        return Command(goto=END)

    async def researcher_node(
        state: SupervisorState, config: RunnableConfig
    ) -> Command[Literal["supervisor"]]:
        result = await researcher.ainvoke(
            {"messages": [{"role": "user", "content": state["question"]}]},
            config=config,
        )
        facts = final_ai_text(result)
        return Command(
            goto="supervisor",
            update={
                "research": facts,
                "messages": [AIMessage(content=facts, name="researcher")],
            },
        )

    async def writer_node(
        state: SupervisorState, config: RunnableConfig
    ) -> Command[Literal["supervisor"]]:
        task = f"Вопрос пользователя:\n{state['question']}\n\nФакты researcher:\n{state['research']}"
        result = await writer.ainvoke(
            {"messages": [{"role": "user", "content": task}]},
            config=config,
        )
        answer = final_ai_text(result)
        return Command(
            goto="supervisor",
            update={
                "final_answer": answer,
                "messages": [AIMessage(content=answer, name="writer")],
            },
        )

    workflow = StateGraph(SupervisorState)
    workflow.add_node("supervisor", supervisor)
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("writer", writer_node)
    workflow.add_edge(START, "supervisor")
    return workflow.compile(checkpointer=InMemorySaver(), name="multi_agent_supervisor")


def save_mermaid(app: Any) -> Path:
    mermaid = app.get_graph().draw_mermaid()
    path = ROOT / "docs" / "architecture-multi-agent.md"
    path.write_text(
        "# Архитектура multi-agent эксперимента\n\n"
        "Схема ниже генерируется вызовом `app.get_graph().draw_mermaid()` в "
        "`experiments/multi_agent_langgraph.py`.\n\n"
        f"```mermaid\n{mermaid}\n```\n",
        encoding="utf-8",
    )
    return path


def print_update(update: dict[str, Any]) -> None:
    for node, payload in update.items():
        if not isinstance(payload, dict):
            print(f"[{node}] {payload}")
            continue
        visible = {
            key: value
            for key, value in payload.items()
            if key in {"research", "final_answer", "handoff_count"}
        }
        print(f"[{node}] {visible or 'state updated'}")


async def run() -> None:
    records = []
    try:
        for index, test in enumerate(TEST_QUESTIONS, 1):
            print(f"\n=== Multi-agent {index}/5: {test.id} ===")
            # A fresh saver prevents prior questions from contaminating the next run,
            # while every independent run uses the required experiment thread id.
            app = build_app()
            tracker = UsageTracker()
            config: RunnableConfig = {
                "configurable": {"thread_id": "exp-langgraph"},
                "callbacks": [tracker],
                "recursion_limit": 20,
            }
            initial: SupervisorState = {
                "messages": [],
                "question": test.question,
                "research": "",
                "final_answer": "",
                "handoff_count": 0,
            }
            started_at = time.perf_counter()
            async for update in app.astream(initial, config=config, stream_mode="updates"):
                print_update(cast(dict[str, Any], update))
            latency_ms = (time.perf_counter() - started_at) * 1000
            snapshot = await app.aget_state(config)
            state = snapshot.values
            answer = str(state["final_answer"])
            print(f"Финальный ответ:\n{answer}")
            records.append(
                make_result(
                    implementation="multi-agent",
                    test=test,
                    answer=answer,
                    tracker=tracker,
                    latency_ms=latency_ms,
                    handoff_count=int(state["handoff_count"]),
                )
            )
            if index == 1:
                print(f"Mermaid сохранён: {save_mermaid(app)}")
        save_results("multi-agent", records)
        if await judge_results_if_complete():
            print("\nLLM-судья обновил quality_score для обеих реализаций.")
        else:
            print("\nQuality-score будет рассчитан после прогона второй реализации.")
    finally:
        await close_rag_service()


if __name__ == "__main__":
    asyncio.run(run())
