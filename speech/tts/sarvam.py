"""Sarvam AI TTS adapter (Bulbul) — Indian voices that speak Hindi and Hinglish natively.

Why: the owner found the OpenAI voice's Hindi unusable on live calls (2026-11-03).
Bulbul v4 Flash is built for real-time Indian-language calls; its best-practice guide
ranks `aparna_hi_customer` (female) first for Hindi sales and customer care.

Protocol, traced to Sarvam's docs (docs.sarvam.ai/api-reference/text-to-speech/
convert-stream, fetched 2026-11-03): `POST https://api.sarvam.ai/text-to-speech/stream`,
header `api-subscription-key`, JSON body `text`, `language_code`, `speaker`, `model`,
`pace`, `temperature`, `speech_sample_rate`, `output_audio_codec` ("linear16" listed),
`enable_preprocessing`; the response body is the audio itself, streamed. Whether
`linear16` output carries a WAV header isn't documented, so a RIFF header at the start
is stripped if present. Errors come back as JSON with `error.message`.

24 kHz like the OpenAI voice, so the fallback (speech/tts/sarvam_fallback.py) can
switch between them; voice/session resamples to the phone line's rate.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator

import httpx

from config.settings import get_settings
from speech.tts.base import TTSProvider

logger = logging.getLogger(__name__)

SARVAM_TTS_SAMPLE_RATE_HZ = 24000
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_clients: dict[int, httpx.AsyncClient] = {}


def _client() -> httpx.AsyncClient:
    """One keep-alive client per event loop: a fresh TLS handshake per phrase would add
    ~100-300 ms to every reply."""
    loop_id = id(asyncio.get_running_loop())
    client = _clients.get(loop_id)
    if client is None or client.is_closed:
        s = get_settings()
        client = httpx.AsyncClient(base_url=s.sarvam_api_base_url, timeout=s.sarvam_http_timeout_seconds,
                                   limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120))
        _clients[loop_id] = client
    return client


def strip_wav_header(buffer: bytes) -> tuple[bytes, bool]:
    """(audio, done): drops a RIFF/WAVE header if `buffer` starts with one. `done` is
    False while the header might still be incomplete (keep buffering)."""
    if not buffer.startswith(b"RIFF"):
        return buffer, len(buffer) >= 4
    marker = buffer.find(b"data", 12)
    if marker < 0 or len(buffer) < marker + 8:
        return b"", False
    return buffer[marker + 8:], True


class SarvamTTSProvider(TTSProvider):
    sample_rate_hz = SARVAM_TTS_SAMPLE_RATE_HZ
    backend = "sarvam"

    def __init__(self) -> None:
        s = get_settings()
        self._key = s.sarvam_api_key
        self._model = s.sarvam_tts_model
        self._speaker_hi = s.sarvam_tts_speaker
        self._speaker_en = s.sarvam_tts_speaker_en or s.sarvam_tts_speaker
        self._pace = s.sarvam_tts_pace
        self._temperature = s.sarvam_tts_temperature
        self._active_cancel_events: set[asyncio.Event] = set()

    def _body(self, text: str) -> dict:
        hindi = bool(_DEVANAGARI.search(text))
        return {
            "text": text,
            "language_code": "hi-IN" if hindi else "en-IN",
            "speaker": self._speaker_hi if hindi else self._speaker_en,
            "model": self._model,
            "pace": self._pace,
            "temperature": self._temperature,
            "speech_sample_rate": SARVAM_TTS_SAMPLE_RATE_HZ,
            "output_audio_codec": "linear16",
            "enable_preprocessing": True,
        }

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        cancel_event = asyncio.Event()
        self._active_cancel_events.add(cancel_event)
        loop = asyncio.get_running_loop()
        started = loop.time()
        head = b""
        header_done = False
        carry = b""  # an odd trailing byte: PCM16 samples are 2 bytes
        first = True
        try:
            async with _client().stream("POST", "/text-to-speech/stream", json=self._body(text),
                                        headers={"api-subscription-key": self._key}) as response:
                if response.status_code != 200:
                    detail = (await response.aread())[:300].decode("utf-8", "replace")
                    raise RuntimeError(f"Sarvam TTS HTTP {response.status_code}: {detail}")
                async for chunk in response.aiter_bytes(2400):
                    if cancel_event.is_set():
                        break
                    if not header_done:
                        head += chunk
                        chunk, header_done = strip_wav_header(head)
                        if not header_done:
                            continue
                    chunk = carry + chunk
                    if len(chunk) % 2:
                        chunk, carry = chunk[:-1], chunk[-1:]
                    else:
                        carry = b""
                    if not chunk:
                        continue
                    if first:
                        first = False
                        logger.info("tts_first_audio", extra={"seconds": round(loop.time() - started, 3),
                                                              "chars": len(text), "backend": "sarvam"})
                    yield chunk
        finally:
            self._active_cancel_events.discard(cancel_event)

    async def cancel(self) -> None:
        for event in list(self._active_cancel_events):
            event.set()

    async def close(self) -> None:
        await self.cancel()
