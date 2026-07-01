from __future__ import annotations

import httpx

from app.core.config import get_settings


async def notify_user(chat_id_tg: int, text: str) -> None:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            f"{settings.bot_url.rstrip('/')}/notify",
            json={"chat_id": chat_id_tg, "text": text},
            headers={"X-Internal-Token": settings.internal_token},
        )
        response.raise_for_status()
