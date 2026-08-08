from __future__ import annotations

import json
import operator
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict
from urllib.parse import quote
from zoneinfo import ZoneInfo

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from app.core.config import Settings, get_settings
from app.services.notifier import notify_user

MAX_ITERATIONS = 6
DANGEROUS_TOOL_NAME = "send_telegram_message"
SYSTEM_PROMPT = (
    "Ты ReAct-агент поддержки PR-review. Используй tools только по "
    "необходимости и не выдумывай данные. Отправка Telegram-сообщения "
    "является опасным действием: сформируй один tool call с точным chat_id "
    "и готовым текстом; граф сам запросит подтверждение."
)


@tool
def search_knowledge_base(query: str) -> str:
    """Search the internal code-review knowledge base."""

    from app.services.rag import search_top_fragment

    return search_top_fragment(query)


@tool
def get_current_time(timezone: str = "Europe/Moscow") -> str:
    """Return current time in an IANA timezone."""

    return datetime.now(ZoneInfo(timezone)).isoformat()


@tool(DANGEROUS_TOOL_NAME)
def send_telegram_message(chat_id: int, text: str) -> str:
    """Send text to Telegram after mandatory human approval."""

    # Schema-only tool: the graph routes this name to its guarded nodes and
    # never invokes this function body.
    raise RuntimeError("dangerous tool must be executed through the approval node")


TOOLS: list[BaseTool] = [
    search_knowledge_base,
    get_current_time,
    send_telegram_message,
]
SAFE_TOOLS = {
    search_knowledge_base.name: search_knowledge_base,
    get_current_time.name: get_current_time,
}


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    iteration_count: NotRequired[int]
    tool_results: Annotated[list[dict[str, Any]], operator.add]
    pending_action: NotRequired[dict[str, Any] | None]
    sent: NotRequired[bool]
    decision: NotRequired[bool | None]
    delivery_result: NotRequired[str | None]


Sender = Callable[[int, str], Awaitable[None]]


def _build_model(settings: Settings) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    model_options: dict[str, Any] = {}
    if "11434" in settings.openai_base_url:
        model_options["extra_body"] = {"think": False}
    return ChatOpenAI(
        model=settings.default_model,
        temperature=0,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=settings.request_timeout,
        **model_options,
    )


def _serialize_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def _last_tool_call(state: AgentState) -> dict[str, Any]:
    calls = list(getattr(state["messages"][-1], "tool_calls", None) or [])
    if len(calls) != 1:
        raise ValueError("agent must request exactly one tool per model step")
    return calls[0]


def build_agent(
    checkpointer: BaseCheckpointSaver[Any],
    *,
    model: BaseChatModel | Any | None = None,
    sender: Sender = notify_user,
    settings: Settings | None = None,
):
    """Compile the persistent ReAct graph around the supplied checkpointer."""

    selected_model = model or _build_model(settings or get_settings())
    bound_model = selected_model.bind_tools(TOOLS)

    async def call_model(state: AgentState) -> dict[str, Any]:
        response = await bound_model.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        )
        return {
            "messages": [response],
            "iteration_count": state.get("iteration_count", 0) + 1,
        }

    def route_after_model(
        state: AgentState,
    ) -> Literal["prepare", "execute", "finish"]:
        calls = list(getattr(state["messages"][-1], "tool_calls", None) or [])
        if not calls or state.get("iteration_count", 0) >= MAX_ITERATIONS:
            return "finish"
        if len(calls) == 1 and calls[0].get("name") == DANGEROUS_TOOL_NAME:
            return "prepare"
        return "execute"

    async def execute_safe_tool(state: AgentState) -> dict[str, Any]:
        call = _last_tool_call(state)
        name = str(call.get("name", ""))
        args = call.get("args") or {}
        call_id = str(call.get("id") or "missing-tool-call-id")
        selected_tool = SAFE_TOOLS.get(name)
        error: str | None = None
        if selected_tool is None:
            error = f"unknown or guarded tool {name!r}"
            content = f"error: {error}"
        else:
            try:
                content = _serialize_result(await selected_tool.ainvoke(args))
            except Exception as exc:  # noqa: BLE001 - failure becomes an observation
                error = f"{type(exc).__name__}: {exc}"
                content = f"error executing tool {name!r}: {error}"
        return {
            "messages": [ToolMessage(content=content, tool_call_id=call_id, name=name)],
            "tool_results": [
                {"name": name, "args": args, "result": content, "error": error}
            ],
        }

    async def prepare_telegram_message(state: AgentState) -> dict[str, Any]:
        """Validate and render the payload without performing a side effect."""

        call = _last_tool_call(state)
        args = call.get("args") or {}
        chat_id = int(args["chat_id"])
        text = str(args["text"]).strip()
        if not text:
            raise ValueError("Telegram message text must not be blank")
        call_id = str(call.get("id") or "missing-tool-call-id")
        payload = {
            "request_id": call_id,
            "tool_call_id": call_id,
            "chat_id": chat_id,
            "text": text,
            "preview": f"Telegram → {chat_id}: {text}",
        }
        return {
            "pending_action": payload,
            "sent": False,
            "decision": None,
            "delivery_result": None,
        }

    async def confirm_and_execute_telegram_message(
        state: AgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """Pause for approval, then and only then call the external API."""

        payload = state["pending_action"]
        if payload is None:
            raise ValueError("pending Telegram payload is missing")

        role = str(
            config.get("configurable", {}).get("user_role", "write-with-approve")
        )
        if role == "full":
            approved = True
        elif role == "read-only":
            approved = False
        else:
            approved = bool(
                interrupt(
                    {
                        "type": "approve_telegram_message",
                        "preview": payload["preview"],
                        "request_id": payload["request_id"],
                    }
                )
            )

        call_id = str(payload["tool_call_id"])
        if not approved:
            result = "Telegram message rejected; no external API call was made."
            return {
                "messages": [
                    ToolMessage(
                        content=result,
                        tool_call_id=call_id,
                        name=DANGEROUS_TOOL_NAME,
                    )
                ],
                "tool_results": [
                    {
                        "name": DANGEROUS_TOOL_NAME,
                        "args": {
                            "chat_id": payload["chat_id"],
                            "text": payload["text"],
                        },
                        "result": result,
                        "error": None,
                    }
                ],
                "decision": False,
                "sent": False,
                "delivery_result": result,
            }

        await sender(int(payload["chat_id"]), str(payload["text"]))
        result = f"Telegram message sent to {payload['chat_id']}."
        return {
            "messages": [
                ToolMessage(
                    content=result,
                    tool_call_id=call_id,
                    name=DANGEROUS_TOOL_NAME,
                )
            ],
            "tool_results": [
                {
                    "name": DANGEROUS_TOOL_NAME,
                    "args": {"chat_id": payload["chat_id"], "text": payload["text"]},
                    "result": result,
                    "error": None,
                }
            ],
            "decision": True,
            "sent": True,
            "delivery_result": result,
        }

    builder = StateGraph(AgentState)
    builder.add_node("call_model", call_model)
    builder.add_node("execute_safe_tool", execute_safe_tool)
    builder.add_node("prepare_telegram_message", prepare_telegram_message)
    builder.add_node(
        "confirm_and_execute_telegram_message",
        confirm_and_execute_telegram_message,
    )
    builder.add_edge(START, "call_model")
    builder.add_conditional_edges(
        "call_model",
        route_after_model,
        {
            "prepare": "prepare_telegram_message",
            "execute": "execute_safe_tool",
            "finish": END,
        },
    )
    builder.add_edge("execute_safe_tool", "call_model")
    builder.add_edge(
        "prepare_telegram_message",
        "confirm_and_execute_telegram_message",
    )
    builder.add_edge("confirm_and_execute_telegram_message", "call_model")
    return builder.compile(checkpointer=checkpointer)


def postgres_connection_uri(settings: Settings) -> str:
    """Build a psycopg v3 URI from the application's POSTGRES_* settings."""

    user = quote(settings.postgres_user, safe="")
    password = quote(settings.postgres_password.get_secret_value(), safe="")
    database = quote(settings.postgres_db, safe="")
    return (
        f"postgresql://{user}:{password}@{settings.postgres_host}:"
        f"{settings.postgres_port}/{database}"
    )


@asynccontextmanager
async def agent_lifespan(
    settings: Settings | None = None,
) -> AsyncIterator[Any]:
    """Create, set up exactly once, and close the configured checkpointer."""

    selected = settings or get_settings()
    backend = selected.agent_checkpointer

    if backend == "memory":
        yield build_agent(InMemorySaver(), settings=selected)
        return

    checkpointer_context: AbstractAsyncContextManager[Any]
    if backend == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        sqlite_path = Path(selected.agent_sqlite_path)
        if str(sqlite_path) != ":memory:":
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        checkpointer_context = AsyncSqliteSaver.from_conn_string(str(sqlite_path))
    elif backend == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpointer_context = AsyncPostgresSaver.from_conn_string(
            postgres_connection_uri(selected)
        )
    else:
        raise ValueError(f"Unsupported AGENT_CHECKPOINTER: {backend!r}")

    async with checkpointer_context as checkpointer:
        await checkpointer.setup()
        yield build_agent(checkpointer, settings=selected)
