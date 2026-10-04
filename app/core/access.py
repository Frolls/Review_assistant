"""Service authentication and user isolation for trusted interface adapters."""
from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse

from app.chat.deps import get_repository


async def access_middleware(request: Request, call_next):
    path = request.url.path
    if path in {"/health", "/live", "/ready", "/docs", "/openapi.json", "/redoc"}:
        return await call_next(request)
    settings = request.app.state.settings
    if path.startswith("/chats/admin"):
        # Admin routes have their own dependency and never trust X-User-ID alone.
        return await call_next(request)
    token = request.headers.get("X-Internal-Token", "")
    if not token or not secrets.compare_digest(token, settings.internal_token):
        return JSONResponse(status_code=401, content={"detail": "Service authentication required"})
    owner = request.headers.get("X-User-ID", "")
    if not owner or len(owner) > 128:
        return JSONResponse(status_code=401, content={"detail": "Authenticated user identity required"})
    request.state.owner_id = owner
    if path.startswith("/documents"):
        admin_token = settings.admin_token
        if hasattr(admin_token, "get_secret_value"):
            admin_token = admin_token.get_secret_value()
        if not secrets.compare_digest(request.headers.get("X-Admin-Token", ""), admin_token or ""):
            return JSONResponse(status_code=403, content={"detail": "Administrator access required"})
    if path == "/chats" and request.method == "POST":
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})
        if payload.get("owner_external_id") != owner or payload.get("system_prompt") is not None:
            return JSONResponse(status_code=403, content={"detail": "Invalid chat owner or system prompt override"})
    if path.startswith("/chats/"):
        try:
            chat_id = UUID(path.split("/")[2])
        except ValueError:
            return JSONResponse(status_code=404, content={"detail": "Chat not found"})
        async for repo in get_repository(request):
            chat = await repo.get_chat(chat_id)
            if chat is None or chat.owner_external_id != owner:
                return JSONResponse(status_code=404, content={"detail": "Chat not found"})
    return await call_next(request)
