from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.services.agent_persistent import build_agent


class OfflineModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="Готово.")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "send_telegram_message",
                    "args": {"chat_id": 4242, "text": "PR #42 готов к review"},
                    "id": "demo-pr-42",
                }
            ],
        )


def input_state() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="Отправь статус PR #42")],
        "iteration_count": 0,
        "tool_results": [],
    }


def config(thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "user_role": "write-with-approve",
        }
    }


async def main() -> None:
    deliveries: list[tuple[int, str]] = []

    async def offline_sender(chat_id: int, text: str) -> None:
        deliveries.append((chat_id, text))

    async with AsyncSqliteSaver.from_conn_string(":memory:") as checkpointer:
        await checkpointer.setup()
        graph = build_agent(
            checkpointer,
            model=OfflineModel(),
            sender=offline_sender,
        )

        approve_config = config("demo-approve-pr-42")
        reject_config = config("demo-reject-pr-42")

        paused = await graph.ainvoke(input_state(), approve_config)
        interrupt_payload = paused["__interrupt__"][0].value
        print("1) INTERRUPT PAYLOAD")
        print(json.dumps(interrupt_payload, ensure_ascii=False, indent=2))

        history = [state async for state in graph.aget_state_history(approve_config)]
        print("\n2) CHECKPOINT HISTORY (newest first)")
        print("checkpoint_id                           | next")
        print(
            "----------------------------------------+----------------------------------------"
        )
        for snapshot in history[:5]:
            checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
            print(f"{checkpoint_id:<39} | {','.join(snapshot.next) or '-'}")

        before_send = next(
            snapshot
            for snapshot in history
            if snapshot.next == ("confirm_and_execute_telegram_message",)
        )
        old_id = before_send.config["configurable"]["checkpoint_id"]
        old_snapshot = await graph.aget_state(
            {
                "configurable": {
                    "thread_id": approve_config["configurable"]["thread_id"],
                    "checkpoint_id": old_id,
                }
            }
        )
        print("\n3) PAST CHECKPOINT BEFORE SEND")
        print(
            json.dumps(
                {
                    "checkpoint_id": old_id,
                    "draft": old_snapshot.values["pending_action"],
                    "sent": old_snapshot.values["sent"],
                    "next": old_snapshot.next,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        # A different thread lineage is intentional. A resume value is a pending
        # write and cannot be replaced by resuming one interrupt twice differently.
        await graph.ainvoke(input_state(), reject_config)
        rejected = await graph.ainvoke(Command(resume=False), reject_config)
        approved = await graph.ainvoke(Command(resume=True), approve_config)

        print("\n4) TWO BRANCHES FROM IDENTICAL INPUT (different thread_id)")
        print(
            json.dumps(
                {
                    "rejected": {
                        "thread_id": reject_config["configurable"]["thread_id"],
                        "decision": rejected["decision"],
                        "sent": rejected["sent"],
                    },
                    "approved": {
                        "thread_id": approve_config["configurable"]["thread_id"],
                        "decision": approved["decision"],
                        "sent": approved["sent"],
                    },
                    "external_calls": deliveries,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
