from __future__ import annotations

from pydantic import BaseModel, Field


class ModelCard(BaseModel):
    id: str
    provider: str = "openai"
    input_price_per_1m_tokens_usd: float = Field(ge=0)
    cached_input_price_per_1m_tokens_usd: float = Field(ge=0)
    output_price_per_1m_tokens_usd: float = Field(ge=0)


MODELS_CATALOG = [
    ModelCard(
        id="gpt-4.1",
        input_price_per_1m_tokens_usd=2.00,
        cached_input_price_per_1m_tokens_usd=0.50,
        output_price_per_1m_tokens_usd=8.00,
    ),
    ModelCard(
        id="gpt-4.1-mini",
        input_price_per_1m_tokens_usd=0.40,
        cached_input_price_per_1m_tokens_usd=0.10,
        output_price_per_1m_tokens_usd=1.60,
    ),
    ModelCard(
        id="gpt-4o-mini",
        input_price_per_1m_tokens_usd=0.15,
        cached_input_price_per_1m_tokens_usd=0.075,
        output_price_per_1m_tokens_usd=0.60,
    ),
]
