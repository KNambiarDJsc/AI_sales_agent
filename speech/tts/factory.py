"""Picks the TTS provider from settings.ai_backend (see llm/openai_client.py). A new
instance per call/session: providers track their own in-flight syntheses for
barge-in cancellation. The heavy model (Kokoro) is shared underneath."""
from __future__ import annotations

from config.settings import get_settings
from speech.tts.base import TTSProvider


def get_tts_provider() -> TTSProvider:
    settings = get_settings()
    mode = settings.ai_backend
    if settings.sarvam_api_key and mode != "local":
        # Hindi-native voice; OpenAI takes over by itself if Sarvam fails.
        from speech.tts.sarvam_fallback import SarvamFallbackTTSProvider

        return SarvamFallbackTTSProvider()
    if mode == "openai":
        from speech.tts.openai import OpenAITTSProvider

        return OpenAITTSProvider()
    if mode == "local":
        from speech.tts.kokoro_local import KokoroTTSProvider

        return KokoroTTSProvider()
    from speech.tts.fallback import FallbackTTSProvider

    return FallbackTTSProvider()
