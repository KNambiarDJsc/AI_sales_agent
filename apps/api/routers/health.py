from __future__ import annotations

import importlib.util

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from database.session import get_session
from llm.openai_client import openai_configured, openai_down_reason, use_openai

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/db")
async def health_db(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "reachable"}


@router.get("/health/ai")
async def health_ai() -> dict:
    """Which backend each stage would use right now, and whether the local backup is
    actually ready. Makes no billable calls (OpenAI state comes from the startup
    probe and from real requests)."""
    s = get_settings()
    from speech.tts.kokoro_local import model_paths

    ollama: dict = {"url": s.local_llm_base_url, "model": s.local_llm_model}
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            tags = (await client.get(f"{s.local_llm_base_url.rstrip('/')}/api/tags")).json()
        names = {m.get("name") for m in tags.get("models", [])}
        ollama["reachable"] = True
        ollama["model_installed"] = s.local_llm_model in names or f"{s.local_llm_model}:latest" in names
    except Exception as exc:  # noqa: BLE001
        ollama.update(reachable=False, model_installed=False, error=type(exc).__name__)
    kokoro_model, kokoro_voices = model_paths()
    local_ready = {
        "stt_moonshine": importlib.util.find_spec("moonshine_onnx") is not None,
        "tts_kokoro": kokoro_model.exists() and kokoro_voices.exists() and importlib.util.find_spec("kokoro_onnx") is not None,
        "llm_ollama": bool(ollama.get("reachable") and ollama.get("model_installed")),
    }
    backend = "openai" if use_openai() else "local"
    return {
        "mode": s.ai_backend,
        "components": {"stt": backend, "llm": backend, "tts": backend},
        "openai": {"configured": openai_configured(), "usable": use_openai(), "reason": openai_down_reason()},
        "local": {"ready": local_ready, "ollama": ollama, "stt_model": f"moonshine/{s.local_stt_model}",
                  "tts_voice": s.local_tts_voice},
    }
