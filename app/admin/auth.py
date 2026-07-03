from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.deps.providers import SettingsDep


async def require_admin(
    settings: SettingsDep,
    x_admin_token: Annotated[str, Header(alias="X-Admin-Token")] = "",
) -> None:
    if not secrets.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail={"code": "admin_unauthorized"})
