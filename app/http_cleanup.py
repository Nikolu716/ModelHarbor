"""Cancellation-safe, bounded cleanup for upstream HTTP resources."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)
CLOSE_TIMEOUT = 5.0


async def close_resource(resource) -> None:
    if resource is None:
        return
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT):
            await resource.aclose()
    except Exception as exc:
        log.warning("Upstream cleanup failed (%s)", type(exc).__name__)


class SafeResponseStream(httpx.AsyncByteStream):
    """Protect even HTTPX's implicit close at EOF from caller cancellation."""

    def __init__(self, stream, start_cleanup):
        self.stream = stream
        self.start_cleanup = start_cleanup
        self.close_task = None

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        if self.close_task is None:
            self.close_task = self.start_cleanup(close_resource(self.stream))
        await asyncio.shield(self.close_task)


def protect_response(response, start_cleanup):
    if not isinstance(response.stream, SafeResponseStream):
        response.stream = SafeResponseStream(response.stream, start_cleanup)
