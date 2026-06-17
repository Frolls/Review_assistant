from __future__ import annotations

from app.schemas.chat import Usage
from app.schemas.models import MODELS_CATALOG, ModelCard


def _model_card(model: str) -> ModelCard:
    for card in MODELS_CATALOG:
        if card.id == model:
            return card
    raise ValueError(f"Unknown model pricing: {model}")


def estimate_chat_cost_usd(
    *,
    model: str,
    usage: Usage,
    cached_input: bool = False,
) -> float:
    card = _model_card(model)
    input_price = (
        card.cached_input_price_per_1m_tokens_usd
        if cached_input
        else card.input_price_per_1m_tokens_usd
    )
    input_cost = usage.prompt_tokens / 1_000_000 * input_price
    output_cost = usage.completion_tokens / 1_000_000 * card.output_price_per_1m_tokens_usd
    return round(input_cost + output_cost, 12)
