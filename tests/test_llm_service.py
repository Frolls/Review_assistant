import unittest
from json import loads
from types import SimpleNamespace

from redis.exceptions import RedisError

from app.core.config import Settings
from app.routers.health import healthcheck, readiness_check
from app.schemas.chat import ChatRequest
from app.services.llm import LLMService


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, ttl, value):
        self.data[key] = value

    async def ping(self):
        return True


class FailingRedis:
    async def ping(self):
        raise RedisError("redis unavailable")


class FakeOpenAIResponse:
    def __init__(self, content: str, model: str = "fallback-model"):
        self.model = model
        self.usage = SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8)
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ]


class SuccessCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class SuccessClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=SuccessCompletions(responses))


class FakeStreamChunk:
    def __init__(self, content=None, usage=None):
        self.choices = [] if content is None else [
            SimpleNamespace(delta=SimpleNamespace(content=content))
        ]
        self.usage = usage


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class StreamCompletions:
    def __init__(self, stream):
        self.stream = stream

    async def create(self, **kwargs):
        return self.stream


class StreamClient:
    def __init__(self, stream):
        self.chat = SimpleNamespace(completions=StreamCompletions(stream))


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_support_openai_compatible_backend(self):
        settings = Settings(
            OPENAI_API_KEY="proxy-key",
            OPENAI_BASE_URL="http://localhost:4000",
            DEFAULT_MODEL="gpt-4.1-mini",
        )
        self.assertEqual(settings.openai_api_key.get_secret_value(), "proxy-key")
        self.assertEqual(settings.openai_base_url, "http://localhost:4000")
        self.assertEqual(settings.default_model, "gpt-4.1-mini")

    async def test_env_example_defaults_are_valid(self):
        settings = Settings(OPENAI_API_KEY="proxy-key")
        self.assertEqual(settings.redis_url, "redis://localhost:6379/0")


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthcheck_returns_ok(self):
        response = await healthcheck()

        self.assertEqual(response.status, "ok")

    async def test_readiness_returns_ok_when_redis_is_available(self):
        response = await readiness_check(FakeRedis())

        self.assertEqual(response.status, "ok")
        self.assertEqual(response.redis, "up")

    async def test_readiness_returns_503_when_redis_is_unavailable(self):
        response = await readiness_check(FailingRedis())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            loads(response.body),
            {"status": "degraded", "redis": "down"},
        )


class LLMServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_uses_cache_after_first_request(self):
        settings = Settings(OPENAI_API_KEY="proxy-key")
        client = SuccessClient([FakeOpenAIResponse("cached answer", model="gpt-4.1-mini")])
        service = LLMService(
            openai=client,
            cache=FakeRedis(),
            settings=settings,
        )

        response = await service.complete(
            ChatRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=32)
        )
        cached = await service.complete(
            ChatRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=32)
        )

        self.assertEqual(response.content, "cached answer")
        self.assertEqual(response.model, "gpt-4.1-mini")
        self.assertFalse(response.cached)
        self.assertTrue(cached.cached)
        self.assertEqual(client.chat.completions.calls, 1)

    async def test_stream_emits_content_and_usage(self):
        settings = Settings(OPENAI_API_KEY="proxy-key")
        stream = FakeStream(
            [
                FakeStreamChunk(content="one "),
                FakeStreamChunk(content="two"),
                FakeStreamChunk(
                    usage=SimpleNamespace(prompt_tokens=2, completion_tokens=2, total_tokens=4)
                ),
            ]
        )
        service = LLMService(
            openai=StreamClient(stream),
            cache=FakeRedis(),
            settings=settings,
        )

        req = ChatRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=32)
        deltas = [delta async for delta in service.stream(req)]

        self.assertEqual(
            [delta.content for delta in deltas if delta.content is not None],
            ["one ", "two"],
        )
        usage_delta = next(delta for delta in deltas if delta.usage is not None)
        self.assertEqual(usage_delta.usage.total_tokens, 4)
