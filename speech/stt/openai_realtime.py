"""OpenAI Realtime API STT adapter — genuine streaming transcription.

Unlike `speech/stt/openai.py` (buffered-per-utterance REST), this gets partial
transcripts while the customer is still talking and a final transcript almost
immediately after our own endpointing calls `receive_final()` — no waiting for a
whole-utterance upload+transcribe round trip. This is the "next highest-leverage
latency change" flagged in STATUS.md's earlier pass.

The protocol below was **confirmed against a live key**, not assembled from
documentation alone — OpenAI's docs disagreed with themselves across pages (a search
result mentioned `?intent=transcription` in the connection URL; the dedicated
client-events/session reference pages omit it entirely). Empirically: connecting
without it (or a `model=` param) gets an immediate `missing_model` error and the
socket closes. See STATUS.md's "Realtime STT migration" section for the full
verification transcript, including confirming the sample-rate floor below.

Protocol summary (verified 2026-09-28):
- Connect: `wss://api.openai.com/v1/realtime?intent=transcription`, header
  `Authorization: Bearer <key>`. No `OpenAI-Beta` header — this endpoint is GA.
- Server sends `session.created` first (default config has its own server-side VAD
  enabled). We immediately send `session.update` setting `turn_detection: null` — we
  do our own VAD/endpointing (Section 17: `voice/vad` + `voice/turn_taking`) and decide
  when to finalize a turn ourselves; leaving the server's VAD active too would be two
  competing endpointing decisions.
- **Audio format floor is 24kHz PCM16.** 16kHz — the rate our own VAD runs at — is
  rejected outright ("integer below minimum value... Expected a value >= 24000").
  `voice/session/session.py` therefore resamples to this adapter's declared
  `input_sample_rate_hz` (24000) independently of the 16kHz feed it resamples for its
  own VAD — both derived from the same original 8kHz mu-law audio, not one from the
  other.
- Audio in: `input_audio_buffer.append` with base64 PCM16 in the `audio` field.
- Turn end: `input_audio_buffer.commit` (manual, since server VAD is disabled) — sent
  from `receive_final()` itself, so the interface contract stays identical to the
  buffered adapter's: "finalize this turn's audio and get the result."
- Partial transcript: `conversation.item.input_audio_transcription.delta` (`delta`).
- Final transcript: `conversation.item.input_audio_transcription.completed`
  (`transcript`).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import websockets

from config.settings import get_settings
from speech.stt.base import STTProvider, STTStream, TranscriptResult

logger = logging.getLogger(__name__)

REALTIME_STT_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
REALTIME_STT_SAMPLE_RATE_HZ = 24000  # confirmed hard floor, not a preference
FINAL_TRANSCRIPT_TIMEOUT_SECONDS = 10.0


class OpenAIRealtimeSTTStream(STTStream):
    def __init__(self, api_key: str, model: str):
        self._api_key = api_key
        self._model = model
        self._ws: websockets.ClientConnection | None = None
        self._reader_task: asyncio.Task | None = None
        self._partial_text = ""
        self._partial_updated = asyncio.Event()
        self._final_queue: asyncio.Queue[TranscriptResult] = asyncio.Queue()
        self._closed = False

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            REALTIME_STT_URL,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=10,
        )
        await self._ws.recv()  # session.created — nothing we need from it
        await self._ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": REALTIME_STT_SAMPLE_RATE_HZ},
                                "transcription": {"model": self._model},
                                "turn_detection": None,
                            }
                        },
                    },
                }
            )
        )
        ack = json.loads(await self._ws.recv())
        if ack.get("type") == "error":
            await self._ws.close()
            raise RuntimeError(f"Realtime session.update rejected: {ack.get('error')}")
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "conversation.item.input_audio_transcription.delta":
                    self._partial_text += event.get("delta", "")
                    self._partial_updated.set()
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    text = (event.get("transcript") or "").strip()
                    self._partial_text = ""
                    await self._final_queue.put(TranscriptResult(text=text, is_final=True) if text else None)
                elif event_type == "error":
                    logger.warning("realtime_stt_server_error", extra={"error": event.get("error")})
        except websockets.ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001 - a dead read loop must not crash the call
            logger.exception("realtime_stt_read_loop_failed")
        finally:
            # Unblock anyone waiting on receive_final() if the socket died mid-turn.
            await self._final_queue.put(None)

    async def send_audio(self, pcm16_chunk: bytes) -> None:
        if self._closed or self._ws is None:
            raise RuntimeError("Realtime STT stream is not connected")
        await self._ws.send(
            json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm16_chunk).decode("ascii")})
        )

    async def receive_partial(self) -> TranscriptResult | None:
        if not self._partial_updated.is_set():
            return None
        self._partial_updated.clear()
        text = self._partial_text
        return TranscriptResult(text=text, is_final=False) if text else None

    async def receive_final(self) -> TranscriptResult | None:
        if self._closed or self._ws is None:
            return None
        try:
            await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except websockets.ConnectionClosed:
            return None
        try:
            return await asyncio.wait_for(self._final_queue.get(), timeout=FINAL_TRANSCRIPT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("realtime_stt_final_timeout")
            return None

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._ws is not None:
            await self._ws.close()


class OpenAIRealtimeSTTProvider(STTProvider):
    input_sample_rate_hz = REALTIME_STT_SAMPLE_RATE_HZ

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.openai_api_key
        self._model = settings.openai_stt_model

    async def start_stream(self) -> STTStream:
        stream = OpenAIRealtimeSTTStream(self._api_key, self._model)
        await stream.connect()
        return stream
