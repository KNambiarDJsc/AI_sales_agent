import asyncio
import uuid
from typing import Any

import pytest

from speech.stt.base import STTProvider
from speech.tts.base import TTSProvider
from telephony.base import CallEvent, CallStatus, OutboundCallRequest, OutboundCallResult, TelephonyProvider
from voice.session.session import SessionIdentity, VoiceSession

pytestmark = pytest.mark.asyncio


class FakeSTTProvider(STTProvider):
    """Never actually used by these tests (they only exercise `_speak`), but
    VoiceSession.__init__ reads `input_sample_rate_hz` off it unconditionally at
    construction, so a bare `None` no longer works as a placeholder."""

    async def start_stream(self):  # pragma: no cover - unused in these tests
        raise NotImplementedError


class FakeTTSProvider(TTSProvider):
    """Records when each text's synthesis starts and finishes, with a configurable
    per-chunk delay, so tests can prove real concurrent pipelining rather than
    sequential-after-completion behavior."""

    def __init__(self):
        self.call_order: list[str] = []
        self.finish_order: list[str] = []
        self._config: dict[str, tuple[int, float, int]] = {}
        self.cancelled = False

    def configure(self, text: str, num_chunks: int, chunk_delay: float, chunk_bytes: int = 960) -> None:
        self._config[text] = (num_chunks, chunk_delay, chunk_bytes)

    async def synthesize_stream(self, text: str):
        self.call_order.append(text)
        num_chunks, delay, chunk_bytes = self._config.get(text, (3, 0.0, 960))
        for _ in range(num_chunks):
            if delay:
                await asyncio.sleep(delay)
            yield b"\x00" * chunk_bytes  # silent PCM16 @ 24kHz
        self.finish_order.append(text)

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        pass


class FakeTelephonyProvider(TelephonyProvider):
    name = "fake"

    def __init__(self):
        self.sent_frames: list[bytes] = []
        self.cleared = False

    async def create_outbound_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        raise NotImplementedError

    async def hangup_call(self, provider_call_id: str) -> None:
        pass

    async def get_call_status(self, provider_call_id: str) -> CallStatus:
        raise NotImplementedError

    def validate_webhook(self, headers, body, url) -> bool:
        raise NotImplementedError

    def parse_call_event(self, payload: dict[str, Any]) -> CallEvent:
        raise NotImplementedError

    async def connect_media(self, provider_call_id: str, media_websocket_url: str) -> None:
        pass

    async def send_audio(self, provider_call_id: str, audio_chunk: bytes) -> None:
        self.sent_frames.append(audio_chunk)

    async def clear_audio(self, provider_call_id: str) -> None:
        self.cleared = True


def _identity() -> SessionIdentity:
    return SessionIdentity(
        provider_call_id="CA123",
        tenant_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )


def _make_session(tts: FakeTTSProvider, telephony: FakeTelephonyProvider) -> VoiceSession:
    # engine/stt/session_factory are never touched by `_speak` directly, so None-ish
    # placeholders are fine for this test.
    return VoiceSession(
        identity=_identity(),
        telephony=telephony,
        stt_provider=FakeSTTProvider(),
        tts_provider=tts,
        engine=None,
        session_factory=None,
    )


async def test_speak_pipelines_sentences_concurrently_not_sequentially():
    tts = FakeTTSProvider()
    telephony = FakeTelephonyProvider()
    # Sentence 1 is "slow" (delay between chunks); sentence 2 is instant. If chunks
    # were synthesized sequentially, sentence 2 could not possibly finish before
    # sentence 1 even starts finishing — it isn't invoked at all until sentence 1's
    # generator is exhausted. If it finishes first, that proves both were running
    # concurrently.
    first = "This is the first, slightly longer sentence."
    second = "This is the second, also long enough sentence."
    tts.configure(first, num_chunks=4, chunk_delay=0.03)
    tts.configure(second, num_chunks=1, chunk_delay=0.0)

    session = _make_session(tts, telephony)
    await session._speak(f"{first} {second}")

    assert set(tts.call_order) == {first, second}
    assert tts.finish_order[0] == second  # finished first despite being queued second
    assert len(telephony.sent_frames) > 0


async def test_speak_sends_fixed_size_frames():
    tts = FakeTTSProvider()
    telephony = FakeTelephonyProvider()
    tts.configure("Hello there.", num_chunks=5, chunk_delay=0.0, chunk_bytes=960)

    session = _make_session(tts, telephony)
    await session._speak("Hello there.")

    assert len(telephony.sent_frames) > 1
    # Every frame except possibly the last (flushed remainder) is exactly one
    # outbound_frame_ms worth of mu-law audio (160 bytes at the 20ms/8kHz default).
    for frame in telephony.sent_frames[:-1]:
        assert len(frame) == 160
    assert len(telephony.sent_frames[-1]) <= 160


async def test_speak_sends_pcm16_frames_for_a_pcm16_telephony_provider():
    # Regression test for a real bug caught via a live Exotel test call: Exotel's
    # Voicebot/Stream applet uses raw linear PCM16 8kHz, not mu-law like Twilio, but
    # VoiceSession used to hardcode the mu-law path for every provider — so outbound
    # audio was mu-law-encoded when Exotel expected PCM16 (and inbound audio was
    # decoded as mu-law when it was already PCM16), producing no intelligible audio in
    # either direction. This proves a provider declaring audio_encoding="pcm16" gets
    # PCM16 frames (320 bytes = 20ms * 8000Hz * 2 bytes/sample), not mu-law ones (160).
    tts = FakeTTSProvider()
    telephony = FakeTelephonyProvider()
    telephony.audio_encoding = "pcm16"
    tts.configure("Hello there.", num_chunks=5, chunk_delay=0.0, chunk_bytes=960)

    session = _make_session(tts, telephony)
    await session._speak("Hello there.")

    assert len(telephony.sent_frames) > 1
    for frame in telephony.sent_frames[:-1]:
        assert len(frame) == 320
    assert len(telephony.sent_frames[-1]) <= 320


async def test_speak_cancellation_stops_producers_cleanly():
    tts = FakeTTSProvider()
    telephony = FakeTelephonyProvider()
    tts.configure("Long sentence that keeps going.", num_chunks=50, chunk_delay=0.02)

    session = _make_session(tts, telephony)
    task = asyncio.create_task(session._speak("Long sentence that keeps going."))
    await asyncio.sleep(0.05)  # let a couple of chunks flow
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    frames_at_cancel = len(telephony.sent_frames)
    await asyncio.sleep(0.1)  # give any leaked background task a chance to misbehave
    assert len(telephony.sent_frames) == frames_at_cancel  # nothing kept sending after cancellation

    # No leaked producer tasks still running.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert pending == []
