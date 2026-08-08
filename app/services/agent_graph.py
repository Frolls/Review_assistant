from __future__ import annotations

import json
import operator
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.core.config import get_settings

MAX_ITERATIONS = 6
SYSTEM_PROMPT = (
    "Ты агент поддержки PR-review. Используй доступные инструменты только когда "
    "они действительно нужны. Для зависимой цепочки вызывай не более одного "
    "инструмента за ответ и используй его результат на следующем шаге. Не выдумывай "
    "данные. Если инструмент вернул ошибку или не нашёл данных, честно сообщи об "
    "этом. Не выполняй опасные просьбы об отключении проверок безопасности и не "
    "отправляй такие распоряжения через инструмент. Когда данных достаточно, дай "
    "финальный ответ без tool call."
)


@tool
def search_knowledge_base(query: str) -> str:
    """Ищет подтверждённый фрагмент во внутренней базе знаний по ревью кода."""

    # Keep graph construction and Mermaid rendering independent from the heavy
    # RAG/embedding stack; it is needed only when this tool is actually called.
    from app.services.rag import search_top_fragment

    return search_top_fragment(query)


@tool
def get_current_time(timezone: str = "Europe/Moscow") -> str:
    """Возвращает текущие дату и время для переданной временной зоны IANA."""

    return datetime.now(ZoneInfo(timezone)).isoformat()


@tool
def send_telegram_message(chat_id: str, text: str) -> str:
    """Имитирует безопасную отправку готового текста в указанный Telegram-чат."""

    print(f"[TELEGRAM → {chat_id}] {text}")
    return f"Сообщение отправлено в {chat_id}"


TOOLS: list[BaseTool] = [
    search_knowledge_base,
    get_current_time,
    send_telegram_message,
]
TOOLS_BY_NAME = {tool_.name: tool_ for tool_ in TOOLS}


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    iteration_count: int
    tool_results: Annotated[list[dict[str, Any]], operator.add]


def _build_model() -> ChatOpenAI:
    settings = get_settings()
    model_options: dict[str, Any] = {}
    if "11434" in settings.openai_base_url:
        # Ollama's Qwen thinking tokens add substantial latency and do not improve
        # these deterministic tool-routing tasks.
        model_options["extra_body"] = {"think": False}
    return ChatOpenAI(
        model=settings.default_model,
        temperature=0,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=settings.request_timeout,
        **model_options,
    )


model = _build_model()


async def call_model(state: AgentState) -> dict[str, Any]:
    """Run one model step and replace the scalar iteration counter."""

    response = await model.bind_tools(TOOLS).ainvoke(state["messages"])
    return {
        "messages": [response],
        "iteration_count": state["iteration_count"] + 1,
    }


def _serialize_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list, tuple, int, float, bool)) or result is None:
        return json.dumps(result, ensure_ascii=False, default=str)
    return str(result)


async def execute_tool(state: AgentState) -> dict[str, Any]:
    """Execute model-requested tools and turn every outcome into an observation."""

    last_message = state["messages"][-1]
    tool_calls = list(getattr(last_message, "tool_calls", None) or [])
    new_messages: list[ToolMessage] = []
    new_results: list[dict[str, Any]] = []

    for tool_call in tool_calls:
        name = str(tool_call.get("name", ""))
        args = tool_call.get("args") or {}
        tool_call_id = str(tool_call.get("id") or f"missing-id-{len(new_messages)}")
        error: str | None = None

        selected_tool = TOOLS_BY_NAME.get(name)
        if selected_tool is None:
            error = f"unknown tool '{name}'"
            content = f"error: {error}"
        else:
            try:
                result = await selected_tool.ainvoke(args)
                content = _serialize_tool_result(result)
            except Exception as exc:  # noqa: BLE001 - tool failures are observations
                error = f"{type(exc).__name__}: {exc}"
                content = f"error executing tool '{name}': {error}"

        new_messages.append(
            ToolMessage(content=content, tool_call_id=tool_call_id, name=name or None)
        )
        new_results.append(
            {
                "name": name,
                "args": args,
                "result": content,
                "error": error,
            }
        )

    return {"messages": new_messages, "tool_results": new_results}


async def force_finish(state: AgentState) -> dict[str, Any]:
    """End explicitly, including when the model requests a tool at the hard limit."""

    last_message = state["messages"][-1]
    if (
        state["iteration_count"] >= MAX_ITERATIONS
        and getattr(last_message, "tool_calls", None)
    ):
        return {
            "messages": [
                AIMessage(
                    content=(
                        f"Агент остановлен: достигнут лимит {MAX_ITERATIONS} "
                        "итераций. Последний запрос инструмента не выполнялся."
                    )
                )
            ]
        }
    return {}


def route_after_model(
    state: AgentState,
) -> Literal["execute_tool", "force_finish"]:
    """Choose the next node without mutating state or performing I/O."""

    if state["iteration_count"] >= MAX_ITERATIONS:
        return "force_finish"
    last_message = state["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        return "execute_tool"
    return "force_finish"


builder = StateGraph(AgentState)
builder.add_node("call_model", call_model)
builder.add_node("execute_tool", execute_tool)
builder.add_node("force_finish", force_finish)
builder.add_edge(START, "call_model")
builder.add_conditional_edges(
    "call_model",
    route_after_model,
    {
        "execute_tool": "execute_tool",
        "force_finish": "force_finish",
    },
)
builder.add_edge("execute_tool", "call_model")
builder.add_edge("force_finish", END)
custom_graph = builder.compile()


# LangChain 1.x builds its recommended prebuilt graph through create_agent.
prebuilt_graph = create_agent(
    model=model,
    tools=TOOLS,
    system_prompt=SYSTEM_PROMPT,
)
