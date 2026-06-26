from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import aiofiles

from app.chat.domain import Chat, ChatMessage


def _use_aiofiles() -> bool:
    if os.getenv("CHAT_JSON_FORCE_SYNC_IO", "").lower() in {"1", "true", "yes"}:
        return False
    # In the Codex sandbox, aiofiles/run_in_executor file opens can hang. Production
    # and ordinary CI should take the aiofiles path required by the storage contract.
    if os.getenv("CODEX_CI"):
        return False
    return True


class _AsyncFile:
    def __init__(self, path: Path, mode: str) -> None:
        self.path = path
        self.mode = mode
        self._file = None

    async def __aenter__(self) -> "_AsyncFile":
        await asyncio.sleep(0)
        self._file = self.path.open(self.mode, encoding="utf-8")
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._file is not None:
            self._file.close()

    async def write(self, data: str) -> int:
        if self._file is None:
            raise RuntimeError("file is not open")
        await asyncio.sleep(0)
        return self._file.write(data)

    async def read(self) -> str:
        if self._file is None:
            raise RuntimeError("file is not open")
        await asyncio.sleep(0)
        return self._file.read()

    async def readlines(self) -> list[str]:
        if self._file is None:
            raise RuntimeError("file is not open")
        await asyncio.sleep(0)
        return self._file.readlines()


@asynccontextmanager
async def _open(path: Path, mode: str = "r") -> AsyncIterator[Any]:
    if _use_aiofiles():
        async with aiofiles.open(path, mode, encoding="utf-8") as file:
            yield file
        return

    async with _AsyncFile(path, mode) as file:
        yield file


class JsonChatRepository:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    async def create_chat(
        self,
        owner_external_id: str,
        interface: str,
        system_prompt: str | None = None,
    ) -> Chat:
        existing_chat = await self._find_chat(owner_external_id, interface, system_prompt)
        if existing_chat is not None:
            return existing_chat

        chat = Chat(
            owner_external_id=owner_external_id,
            interface=interface,
            system_prompt=system_prompt,
        )
        chat_dir = self._chat_dir(chat.id)
        chat_dir.mkdir(parents=True, exist_ok=True)
        async with _open(self._chat_path(chat.id), "w") as file:
            await file.write(chat.model_dump_json())
        return chat

    async def get_chat(self, chat_id: UUID) -> Chat | None:
        path = self._chat_path(chat_id)
        if not path.exists():
            return None
        async with _open(path) as file:
            payload = await file.read()
        return Chat.model_validate_json(payload)

    async def append_message(self, chat_id: UUID, message: ChatMessage) -> ChatMessage:
        if message.chat_id != chat_id:
            raise ValueError("message.chat_id must match chat_id")

        chat_dir = self._chat_dir(chat_id)
        chat_dir.mkdir(parents=True, exist_ok=True)
        async with _open(self._messages_path(chat_id), "a") as file:
            await file.write(message.model_dump_json())
            await file.write("\n")
        return message

    async def list_messages(self, chat_id: UUID, limit: int = 50) -> list[ChatMessage]:
        path = self._messages_path(chat_id)
        if not path.exists():
            return []

        async with _open(path) as file:
            lines = await file.readlines()

        last_soft_delete_index = -1
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "soft_delete":
                last_soft_delete_index = index

        messages: list[ChatMessage] = []
        for line in lines[last_soft_delete_index + 1 :]:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "soft_delete":
                continue
            messages.append(ChatMessage.model_validate_json(line))

        messages.sort(key=lambda item: item.created_at)
        if limit <= 0:
            return []
        return messages[-limit:]

    async def soft_delete_messages(self, chat_id: UUID) -> None:
        if not self._chat_dir(chat_id).exists():
            return

        marker = {
            "type": "soft_delete",
            "at": datetime.now(UTC).isoformat(),
        }
        async with _open(self._messages_path(chat_id), "a") as file:
            await file.write(json.dumps(marker, ensure_ascii=False))
            await file.write("\n")

    def _chat_dir(self, chat_id: UUID) -> Path:
        return self.base_dir / "chats" / str(chat_id)

    def _chat_path(self, chat_id: UUID) -> Path:
        return self._chat_dir(chat_id) / "chat.json"

    def _messages_path(self, chat_id: UUID) -> Path:
        return self._chat_dir(chat_id) / "messages.jsonl"

    async def _find_chat(
        self,
        owner_external_id: str,
        interface: str,
        system_prompt: str | None,
    ) -> Chat | None:
        chats_dir = self.base_dir / "chats"
        if not chats_dir.exists():
            return None

        for chat_path in sorted(chats_dir.glob("*/chat.json")):
            async with _open(chat_path) as file:
                payload = await file.read()
            chat = Chat.model_validate_json(payload)
            if (
                chat.owner_external_id == owner_external_id
                and chat.interface == interface
                and chat.system_prompt == system_prompt
            ):
                return chat
        return None
