"""One shared AsyncOpenAI client per event loop.

Every provider used to build its own client, so each browser session / phone call
opened fresh TLS connections for STT, LLM and TTS — a handshake (~100-300 ms) on the
first request of each, in the middle of the customer's first turn. Sharing one client
keeps its connection pool warm across turns and calls.

Keyed by event loop because an httpx connection pool can't be reused across loops
(tests and scripts run several `asyncio.run` loops in one process; the server has one).
"""
from __future__ import annotations

import asyncio
import weakref

from openai import AsyncOpenAI

from config.settings import get_settings

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncOpenAI]" = weakref.WeakKeyDictionary()
_no_loop_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    global _no_loop_client
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if _no_loop_client is None:
            _no_loop_client = AsyncOpenAI(api_key=get_settings().openai_api_key)
        return _no_loop_client
    client = _clients.get(loop)
    if client is None:
        client = AsyncOpenAI(api_key=get_settings().openai_api_key)
        _clients[loop] = client
    return client
