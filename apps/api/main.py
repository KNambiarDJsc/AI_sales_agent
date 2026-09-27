from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.routers import campaigns, health, leads, media, webhooks
from observability.logging.logging_config import configure_logging
from observability.tracing.tracing import configure_tracing


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    configure_tracing()
    yield


app = FastAPI(title="voice-sales-agent", lifespan=lifespan)

app.include_router(health.router)
app.include_router(campaigns.router)
app.include_router(leads.router)
app.include_router(webhooks.router)
app.include_router(media.router)
