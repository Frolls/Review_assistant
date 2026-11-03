"""Buffered content, live heartbeat, and a deadline for the entire turn."""
import asyncio
import time
from contextlib import suppress


async def with_heartbeat(source, *, heartbeat, timeout_event, deadline=180, interval=10):
    queue = asyncio.Queue(maxsize=1)
    end = object()

    async def produce():
        try:
            async with asyncio.timeout(deadline):
                async for item in source:
                    await queue.put(item)
        except TimeoutError:
            await queue.put(timeout_event)
        except Exception as exc:
            await queue.put(exc)
        finally:
            await source.aclose()
        await queue.put(end)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), interval)
            except TimeoutError:
                yield heartbeat
                continue
            if item is end:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def remaining_turn_time(request) -> float:
    return max(.001, getattr(request.state, "turn_deadline", time.monotonic() + 180) - time.monotonic())
