"""OpenAI STT adapter (Section 8 default).

Design choice: OpenAI's `audio.transcriptions` REST endpoint (used here) does not
stream interim/partial results — only OpenAI's Realtime API websocket protocol does,
and that protocol's exact event shape changes across previews. Per the framework
decision rule (Section 5: reliability + debuggability over framework convenience for
an urgent MVP), this adapter buffers audio per conversational turn and transcribes
once VAD/endpointing (voice/vad, voice/turn_taking) signals the turn is over.
`receive_partial()` therefore always returns None — that's an honest "not supported by
this backend," not a bug. If low-latency partial transcripts become a real requirement,
swap this adapter for one built on the Realtime API's transcription mode without
changing the STTProvider interface or any caller.
"""
from __future__ import annotations

import io
import wave

from openai import AsyncOpenAI

from config.settings import get_settings
from llm.openai_client import get_openai_client
from speech.stt.base import STTProvider, STTStream, TranscriptResult

_SAMPLE_RATE_HZ = 16000
_SAMPLE_WIDTH_BYTES = 2  # PCM16
_CHANNELS = 1


def _pcm16_to_wav_bytes(pcm16_data: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(_CHANNELS)
        wav_file.setsampwidth(_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(_SAMPLE_RATE_HZ)
        wav_file.writeframes(pcm16_data)
    return buffer.getvalue()


class OpenAISTTStream(STTStream):
    def __init__(self, client: AsyncOpenAI | None, model: str, language: str = ""):
        self._client = client
        self._model = model
        self._language = language
        self._buffer = bytearray()
        self._closed = False

    async def send_audio(self, pcm16_chunk: bytes) -> None:
        if self._closed:
            raise RuntimeError("STT stream is closed")
        self._buffer.extend(pcm16_chunk)

    async def receive_partial(self) -> TranscriptResult | None:
        return None  # see module docstring

    async def receive_final(self) -> TranscriptResult | None:
        if not self._buffer:
            return None
        wav_bytes = _pcm16_to_wav_bytes(bytes(self._buffer))
        self._buffer.clear()
        audio_file = io.BytesIO(wav_bytes)
        audio_file.name = "turn.wav"
        extra = {"language": self._language} if self._language else {}
        response = await (self._client or get_openai_client()).audio.transcriptions.create(
            model=self._model,
            file=audio_file,
            response_format="json",
            **extra,
        )
        text = getattr(response, "text", "") or ""
        if not text.strip():
            return None
        return TranscriptResult(text=text.strip(), is_final=True)

    async def close(self) -> None:
        self._closed = True
        self._buffer.clear()


class OpenAISTTProvider(STTProvider):
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.openai_stt_model
        self._language = settings.stt_language

    async def start_stream(self) -> STTStream:
        # Shared, connection-pooled client (llm/openai_client.py), resolved per call.
        return OpenAISTTStream(None, self._model, self._language)
