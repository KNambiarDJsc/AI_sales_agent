from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.routers import campaigns, console, demo, flows, health, leads, media, webhooks
from config.settings import get_settings
from observability.logging.logging_config import configure_logging
from observability.tracing.tracing import configure_tracing

logger = logging.getLogger(__name__)


async def _prepare_ai_backends() -> None:
    """Find out whether OpenAI works (free probe) and load the local backup models,
    so neither the first customer turn nor the first turn after an OpenAI failure
    pays for model loading (Kokoro/Moonshine load in seconds; a cold Ollama model can
    take 10+ s — past the engine's LLM deadline)."""
    from llm.openai_client import probe_openai

    s = get_settings()
    openai_ok = await probe_openai()
    logger.info("ai_backend_selected", extra={"mode": s.ai_backend, "openai_usable": openai_ok})
    if s.ai_backend == "openai" or not s.local_preload:
        return
    from llm.ollama import warm_up_ollama
    from speech.stt.moonshine_local import preload_moonshine
    from speech.tts.kokoro_local import preload_kokoro

    # The speech models are small (~0.5 GB together) and always preloaded. The local
    # LLM holds ~2.5-3 GB while loaded, so it's only loaded up front when it will
    # actually be used; otherwise llm/openai_client.py starts loading it the moment
    # OpenAI first fails.
    loaders = [("moonshine", preload_moonshine), ("kokoro", preload_kokoro)]
    if s.ai_backend == "local" or not openai_ok:
        loaders.append(("ollama", warm_up_ollama))
    for name, loader in loaders:
        try:
            await loader()
            logger.info("local_model_ready", extra={"model": name})
        except Exception as exc:  # noqa: BLE001 - a missing backup must not stop the server
            logger.warning("local_model_unavailable", extra={"model": name, "error": f"{type(exc).__name__}: {exc}"[:300]})


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    configure_tracing()
    warmup = asyncio.create_task(_prepare_ai_backends())
    tasks = [warmup]
    s = get_settings()
    if s.console_dialer_enabled and s.console_password:
        tasks.append(asyncio.create_task(console.run_console_dialer()))
    yield
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


app = FastAPI(title="voice-sales-agent", lifespan=lifespan)
# The client console (web/) calls the API from the browser. Only the configured
# origins; every console route also needs a login token (apps/api/console_auth.py).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().console_allowed_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router)
app.include_router(campaigns.router)
app.include_router(leads.router)
app.include_router(webhooks.router)
app.include_router(flows.router)
app.include_router(media.router)
app.include_router(demo.router)
app.include_router(console.public_router)
app.include_router(console.router)
