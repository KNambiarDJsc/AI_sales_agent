"""Shared OpenAI client + OpenAI health tracking (which decides OpenAI vs local).

Client: one AsyncOpenAI per event loop. Every provider used to build its own client,
so each browser session / phone call opened fresh TLS connections for STT, LLM and
TTS — a handshake (~100-300 ms) on the first request of each, mid-turn. Sharing one
client keeps its connection pool warm. Keyed by event loop because an httpx pool
can't be reused across loops (tests/scripts run several `asyncio.run` loops in one
process; the server has one).

Health: with `settings.ai_backend == "auto"`, the fallback providers
(`llm/fallback.py`, `speech/stt/fallback.py`, `speech/tts/fallback.py`) ask
`use_openai()` before every request. OpenAI is skipped when no key is configured, or
for `openai_retry_after_seconds` after it refused for an *account* reason (invalid /
expired key, no credits, no access) — `is_openai_account_error`. Network failures
also fail over, but are retried sooner. Ordinary per-request errors (a 500, a timeout)
are not account problems and don't flip the switch.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref

import openai
from openai import AsyncOpenAI

from config.settings import get_settings

logger = logging.getLogger(__name__)

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncOpenAI]" = weakref.WeakKeyDictionary()
_no_loop_client: AsyncOpenAI | None = None

NETWORK_RETRY_AFTER_SECONDS = 30.0


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


class _OpenAIHealth:
    def __init__(self) -> None:
        self.down_until = 0.0
        self.reason = ""

    def mark_down(self, reason: str, retry_after: float) -> None:
        first = self.down_until <= time.monotonic()
        self.down_until = time.monotonic() + retry_after
        self.reason = reason
        if first:
            logger.warning("openai_unavailable_using_local", extra={"reason": reason, "retry_after_s": retry_after})
            _start_local_llm_warmup()

    def is_down(self) -> bool:
        return time.monotonic() < self.down_until

    def reset(self) -> None:
        self.down_until = 0.0
        self.reason = ""


_health = _OpenAIHealth()
_warmup_task: "asyncio.Task | None" = None


def _start_local_llm_warmup() -> None:
    """OpenAI just became unavailable: start loading the local LLM now (it isn't kept
    in memory while OpenAI works). The turn that discovered the failure may still be
    slow; the following ones aren't."""
    global _warmup_task
    if get_settings().ai_backend == "openai" or (_warmup_task is not None and not _warmup_task.done()):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from llm.ollama import warm_up_ollama

    async def _warm() -> None:
        try:
            await warm_up_ollama()
            logger.info("local_llm_warmed_after_openai_failure")
        except Exception as exc:  # noqa: BLE001
            logger.warning("local_llm_warmup_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})

    _warmup_task = loop.create_task(_warm())


def openai_configured() -> bool:
    return bool(get_settings().openai_api_key.strip())


def use_openai() -> bool:
    """Should the next request go to OpenAI (True) or the local model (False)?"""
    mode = get_settings().ai_backend
    if mode == "local":
        return False
    if mode == "openai":
        return True
    return openai_configured() and not _health.is_down()


def local_allowed() -> bool:
    return get_settings().ai_backend != "openai"


def openai_down_reason() -> str:
    if not openai_configured():
        return "no OPENAI_API_KEY configured"
    return _health.reason if _health.is_down() else ""


def is_openai_account_error(exc: BaseException) -> bool:
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return True
    if isinstance(exc, openai.RateLimitError) and "insufficient_quota" in str(exc):
        return True
    # AsyncOpenAI refuses to even construct without a key.
    return isinstance(exc, openai.OpenAIError) and "api_key" in str(exc) and not isinstance(exc, openai.APIError)


def note_openai_failure(exc: BaseException) -> bool:
    """Record an OpenAI failure. Returns True if it is one the local backend should
    take over from (account or network problem), False for ordinary request errors."""
    if is_openai_account_error(exc):
        _health.mark_down(f"{type(exc).__name__}: {_short(exc)}", get_settings().openai_retry_after_seconds)
        return True
    if isinstance(exc, openai.APIConnectionError):
        _health.mark_down(f"network: {_short(exc)}", NETWORK_RETRY_AFTER_SECONDS)
        return True
    return False


_KEY_LIKE = re.compile(r"sk-[A-Za-z0-9*_\-]{6,}")


def _short(exc: BaseException) -> str:
    text = str(exc)
    key = get_settings().openai_api_key
    if key:
        text = text.replace(key, "<redacted>")
    # OpenAI echoes a masked key ("sk-proj-****…uqMA") in auth errors; don't spread
    # even that into logs and the health endpoint.
    return _KEY_LIKE.sub("<key>", text)[:160]


async def probe_openai() -> bool:
    """One cheap, free request (list models) to catch a missing/invalid/expired key
    at startup, before a customer's first turn has to discover it. Credit exhaustion
    only shows up on billable calls, so that case is caught at the first real one."""
    if get_settings().ai_backend == "local":
        return False
    if not openai_configured():
        _health.mark_down("no OPENAI_API_KEY configured", get_settings().openai_retry_after_seconds)
        return False
    try:
        await asyncio.wait_for(get_openai_client().models.list(), timeout=8)
    except Exception as exc:  # noqa: BLE001
        if not note_openai_failure(exc):
            logger.warning("openai_probe_inconclusive", extra={"error": _short(exc)})
        return False
    _health.reset()
    return True


def reset_openai_health() -> None:
    _health.reset()
