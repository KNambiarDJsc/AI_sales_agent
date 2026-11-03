"""Sarvam AI streaming STT adapter (Saaras, Indian languages incl. Hindi/Hinglish).

Why: on live Hindi calls the OpenAI transcriber misheard short replies and took 2-12 s
to finalize a turn (config/settings.py, "Sarvam AI"). Saaras is trained on Indian phone
speech; `mode=codemix` writes Hindi words in Devanagari and English words in English
letters — the form the LLM prompt and the Hindi voice expect.

Protocol, traced to Sarvam's docs (docs.sarvam.ai/api-reference/speech-to-text/
transcribe/ws, fetched 2026-11-03) and the official SDK (sarvamai 0.1.42,
speech_to_text_streaming/raw_client.py + socket_client.py):
- Connect: `wss://api.sarvam.ai/speech-to-text/ws` with header
  `Api-Subscription-Key: <key>` and query parameters `language-code` (hyphen, as the
  SDK sends it), `model`, `mode`, `sample_rate`, `flush_signal`.
- Audio in: `{"audio": {"data": <base64>, "sample_rate": "16000", "encoding":
  "audio/wav"}}` — raw PCM16 frames labelled "audio/wav", as the SDK's `transcribe()`
  default and Pipecat's Sarvam service send them.
- Turn end: `{"type": "flush"}` (honoured with `flush_signal=true`) finalizes what has
  been heard so far; we send it when our own endpointing (voice/turn_taking) ends the
  turn, so the interface matches the other STT adapters.
- Out: `{"type": "data", "data": {"transcript": ..., "language_code": ...,
  "metrics": {...}}}` per finalized segment; `{"type": "error", "data": {"error",
  "code"}}`; `{"type": "events", ...}` only with `vad_signals=true` (not used).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.parse

import websockets

from config.settings import get_settings
from speech.stt.base import STTProvider, STTStream, TranscriptResult

logger = logging.getLogger(__name__)

SARVAM_STT_SAMPLE_RATE_HZ = 16000
# Measured against the live service (2026-11-03): with speech still pending, the flush's
# transcript arrived ~0.15 s after the flush. With nothing pending — Sarvam's own VAD
# already sent the segment, or the "turn" was only noise — the flush gets no reply at
# all, so these waits are kept short: every second here is dead air on the call, and
# a noise turn would hold up the next real one.
FINAL_TRANSCRIPT_TIMEOUT_SECONDS = 1.5
_LATE_SEGMENT_SECONDS = 0.3


def stt_url(base_url: str, model: str, mode: str, language: str) -> str:
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    params = {
        "language-code": language or "unknown",
        "model": model,
        "sample_rate": str(SARVAM_STT_SAMPLE_RATE_HZ),
        "flush_signal": "true",
    }
    if mode:
        params["mode"] = mode
    return f"{ws_base}/speech-to-text/ws?{urllib.parse.urlencode(params)}"


class SarvamSTTStream(STTStream):
    def __init__(self, api_key: str, url: str):
        self._api_key = api_key
        self._url = url
        self._ws: websockets.ClientConnection | None = None
        self._reader_task: asyncio.Task | None = None
        self._segments: list[str] = []  # finalized text since the last receive_final()
        self._segment_arrived = asyncio.Event()
        self._closed = False

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            self._url, additional_headers={"Api-Subscription-Key": self._api_key}, open_timeout=10
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                kind = message.get("type")
                data = message.get("data") or {}
                if kind == "data":
                    text = (data.get("transcript") or "").strip()
                    if text:
                        self._segments.append(text)
                    self._segment_arrived.set()
                    metrics = data.get("metrics") or {}
                    logger.info("sarvam_stt_segment", extra={"latency_s": metrics.get("processing_latency"),
                                                              "audio_s": metrics.get("audio_duration"),
                                                              "language": data.get("language_code")})
                elif kind == "error":
                    logger.warning("sarvam_stt_error", extra={"error": data.get("error"), "code": data.get("code")})
        except websockets.ConnectionClosed as exc:
            if not self._closed:
                logger.warning("sarvam_stt_closed", extra={"code": exc.rcvd.code if exc.rcvd else None,
                                                           "reason": exc.rcvd.reason if exc.rcvd else ""})
        except Exception:  # noqa: BLE001 - a dead read loop must not crash the call
            logger.exception("sarvam_stt_read_loop_failed")
        finally:
            self._closed = True
            self._segment_arrived.set()  # unblock a waiting receive_final()

    async def send_audio(self, pcm16_chunk: bytes) -> None:
        if self._closed or self._ws is None:
            raise RuntimeError("Sarvam STT stream is not connected")
        await self._ws.send(json.dumps({"audio": {
            "data": base64.b64encode(pcm16_chunk).decode("ascii"),
            "sample_rate": str(SARVAM_STT_SAMPLE_RATE_HZ),
            "encoding": "audio/wav",
        }}))

    async def receive_partial(self) -> TranscriptResult | None:
        return None

    async def receive_final(self) -> TranscriptResult | None:
        """Flush, then return everything finalized since the previous turn. Waits for
        the flush's segment; if text had already arrived (Sarvam's own segmenting),
        waits only briefly for more."""
        if self._closed or self._ws is None:
            return self._take()
        self._segment_arrived.clear()
        try:
            await self._ws.send(json.dumps({"type": "flush"}))
        except websockets.ConnectionClosed:
            return self._take()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FINAL_TRANSCRIPT_TIMEOUT_SECONDS
        had_text = bool(self._segments)
        try:
            await asyncio.wait_for(
                self._segment_arrived.wait(),
                timeout=_LATE_SEGMENT_SECONDS if had_text else max(deadline - loop.time(), 0.001),
            )
        except asyncio.TimeoutError:
            if not had_text:
                logger.warning("sarvam_stt_final_timeout")
        return self._take()

    def _take(self) -> TranscriptResult | None:
        text = " ".join(self._segments).strip()
        self._segments = []
        return TranscriptResult(text=text, is_final=True) if text else None

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._ws is not None:
            await self._ws.close()


class SarvamSTTProvider(STTProvider):
    input_sample_rate_hz = SARVAM_STT_SAMPLE_RATE_HZ
    backend = "sarvam"

    def __init__(self) -> None:
        s = get_settings()
        self._api_key = s.sarvam_api_key
        self._url = stt_url(s.sarvam_api_base_url, s.sarvam_stt_model, s.sarvam_stt_mode, s.sarvam_stt_language)

    async def start_stream(self) -> STTStream:
        stream = SarvamSTTStream(self._api_key, self._url)
        await stream.connect()
        return stream


class _FailoverSTTStream(STTStream):
    """Sarvam while it works; OpenAI's per-utterance transcriber (same 16 kHz input)
    for the rest of the call once the Sarvam socket fails — a dead transcriber would
    leave the agent deaf for the whole call."""

    def __init__(self, primary: SarvamSTTStream | None, make_secondary):
        self._primary = primary
        self._make_secondary = make_secondary
        self._secondary: STTStream | None = None

    async def _active(self) -> STTStream:
        if self._primary is not None and not self._primary._closed:
            return self._primary
        if self._secondary is None:
            logger.warning("sarvam_stt_failed_over_to_openai")
            self._secondary = await self._make_secondary()
        return self._secondary

    async def send_audio(self, pcm16_chunk: bytes) -> None:
        stream = await self._active()
        try:
            await stream.send_audio(pcm16_chunk)
        except (RuntimeError, websockets.ConnectionClosed):
            if stream is not self._primary:
                raise
            self._primary._closed = True
            await (await self._active()).send_audio(pcm16_chunk)

    async def receive_partial(self) -> TranscriptResult | None:
        return await (await self._active()).receive_partial()

    async def receive_final(self) -> TranscriptResult | None:
        if self._primary is not None and self._secondary is None:
            result = await self._primary.receive_final()
            if result is not None or not self._primary._closed:
                return result
        return await (await self._active()).receive_final()

    async def close(self) -> None:
        for stream in (self._primary, self._secondary):
            if stream is not None:
                await stream.close()


class SarvamWithFallbackSTTProvider(SarvamSTTProvider):
    """Sarvam, falling back to OpenAI (buffered REST) if it can't connect or dies."""

    async def start_stream(self) -> STTStream:
        from speech.stt.openai import OpenAISTTProvider

        async def secondary() -> STTStream:
            return await OpenAISTTProvider().start_stream()

        try:
            primary = SarvamSTTStream(self._api_key, self._url)
            await asyncio.wait_for(primary.connect(), timeout=6)
        except Exception as exc:  # noqa: BLE001 - any connect failure → OpenAI
            logger.warning("sarvam_stt_connect_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:200]})
            primary = None
        return _FailoverSTTStream(primary, secondary)
