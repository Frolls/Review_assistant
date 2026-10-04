from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    redis: str
    dependencies: dict[str, str] = Field(default_factory=dict)
