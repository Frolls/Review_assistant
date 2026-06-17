from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.exceptions import LLMQuotaError
from app.llm.costing import estimate_chat_cost_usd
from app.schemas.chat import ChatRequest, Usage
from app.services.llm import LLMService


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, ttl, value):
        self.data[key] = value


class FakeOpenAIResponse:
    def __init__(self, content: str, model: str = "gpt-4.1-mini"):
        self.model = model
        self.usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=2000, total_tokens=3000)
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ]


class CompletionClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def create(self, **kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        self.last_kwargs = kwargs
        return outcome


class FakeOpenAI:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=CompletionClient(outcomes))


class FakeRateLimit(Exception):
    status_code = 429


class FakeInsufficientQuota(Exception):
    status_code = 429
    code = "insufficient_quota"


def settings() -> Settings:
    return Settings(OPENAI_API_KEY="test-key", DEFAULT_MODEL="gpt-4.1-mini")


def test_estimate_chat_cost_uses_input_and_output_model_prices():
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=500_000, total_tokens=1_500_000)

    cost = estimate_chat_cost_usd(model="gpt-4.1-mini", usage=usage)

    assert cost == 1.2


def test_complete_cache_hit_skips_second_provider_call():
    async def run():
        client = FakeOpenAI([FakeOpenAIResponse("cached answer")])
        service = LLMService(openai=client, cache=FakeRedis(), settings=settings())
        request = ChatRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=32)

        first = await service.complete(request)
        second = await service.complete(request)

        assert first.cached is False
        assert second.cached is True
        assert client.chat.completions.calls == 1

    asyncio.run(run())


def test_complete_cache_miss_calls_provider_with_request_temperature_zero():
    async def run():
        client = FakeOpenAI([FakeOpenAIResponse("fresh answer")])
        service = LLMService(openai=client, cache=FakeRedis(), settings=settings())
        request = ChatRequest(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0,
            max_tokens=32,
        )

        response = await service.complete(request)

        assert response.content == "fresh answer"
        assert client.chat.completions.last_kwargs["temperature"] == 0

    asyncio.run(run())


def test_complete_retries_once_on_429(mocker):
    async def run():
        sleep = mocker.patch("app.services.llm.asyncio.sleep", new=mocker.AsyncMock())
        client = FakeOpenAI(
            [FakeRateLimit("too many requests"), FakeOpenAIResponse("after retry")]
        )
        service = LLMService(openai=client, cache=FakeRedis(), settings=settings())
        request = ChatRequest(messages=[{"role": "user", "content": "retry"}], max_tokens=32)

        response = await service.complete(request)

        assert response.content == "after retry"
        assert client.chat.completions.calls == 2
        sleep.assert_awaited_once()

    asyncio.run(run())


def test_complete_does_not_retry_insufficient_quota(mocker):
    async def run():
        sleep = mocker.patch("app.services.llm.asyncio.sleep", new=mocker.AsyncMock())
        client = FakeOpenAI([FakeInsufficientQuota("insufficient_quota")])
        service = LLMService(openai=client, cache=FakeRedis(), settings=settings())
        request = ChatRequest(messages=[{"role": "user", "content": "quota"}], max_tokens=32)

        with pytest.raises(LLMQuotaError):
            await service.complete(request)

        assert client.chat.completions.calls == 1
        sleep.assert_not_awaited()

    asyncio.run(run())
