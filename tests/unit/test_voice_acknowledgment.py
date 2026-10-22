"""Instant acknowledgments (voice/session/acknowledgments.py + VoiceSession._acknowledge):
played the moment the customer stops, and the reply never interleaves with them."""
import asyncio

import numpy as np
import pytest

from tests.unit.test_voice_session_speak import FakeSTTProvider, FakeTelephonyProvider, _identity
from voice.session import acknowledgments
from voice.session.session import VoiceSession
from voice.session.speech_pipeline import phrases_from

pytestmark = pytest.mark.asyncio

ACK_LEVEL, REPLY_LEVEL = 1000, 3000


class LevelTTS:
    """Constant-level PCM per text, so frames can be told apart after resampling."""

    sample_rate_hz = 24000
    _voice, _model = "test", "test"

    def __init__(self, delay=0.0):
        self.calls: list[str] = []
        self.delay = delay

    async def synthesize_stream(self, text):
        self.calls.append(text)
        level = ACK_LEVEL if text in ("Okay.", "जी।") else REPLY_LEVEL
        for _ in range(4):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield np.full(2400, level, dtype="<i2").tobytes()  # 100 ms

    async def cancel(self):
        pass

    async def close(self):
        pass


class AckEngine:
    def __init__(self, text="Okay."):
        self.text = text

    def acknowledgment(self):
        return self.text

    def all_acknowledgments(self):
        return [self.text]


class Pcm16Telephony(FakeTelephonyProvider):
    audio_encoding = "pcm16"


def _session(tts, telephony, engine):
    return VoiceSession(identity=_identity(), telephony=telephony, stt_provider=FakeSTTProvider(),
                        tts_provider=tts, engine=engine, session_factory=None)


def _levels(frames):
    return [int(np.abs(np.frombuffer(f, dtype="<i2")).mean()) for f in frames if f]


@pytest.fixture(autouse=True)
def _empty_cache():
    acknowledgments._cache.clear()
    yield
    acknowledgments._cache.clear()


async def test_acknowledgment_plays_first_and_the_reply_follows_without_interleaving():
    tts, tel = LevelTTS(), Pcm16Telephony()
    await acknowledgments.prerecord(tts, ["Okay."])
    session = _session(tts, tel, AckEngine())
    session._acknowledge()
    session._start_speaking_phrases(phrases_from(["That's great to hear."]))  # reply arrives mid-acknowledgment
    await session._speaking_task
    levels = _levels(tel.sent_frames)
    ack = [i for i, lv in enumerate(levels) if abs(lv - ACK_LEVEL) < 400]
    reply = [i for i, lv in enumerate(levels) if abs(lv - REPLY_LEVEL) < 400]
    assert ack and reply
    assert max(ack) < min(reply)  # every acknowledgment frame went out before any reply frame
    assert tts.calls.count("Okay.") == 1  # recorded once, never synthesized on the hot path


async def test_barge_in_stops_the_acknowledgment_and_the_queued_reply():
    tts, tel = LevelTTS(), Pcm16Telephony()
    await acknowledgments.prerecord(tts, ["Okay."])
    session = _session(tts, tel, AckEngine())
    session._acknowledge()
    session._start_speaking_phrases(phrases_from(["That's great to hear."]))
    await asyncio.sleep(0.05)
    await session._handle_barge_in()
    sent = len(tel.sent_frames)
    await asyncio.sleep(0.3)
    assert len(tel.sent_frames) == sent  # nothing more after the interruption
    assert REPLY_LEVEL not in [round(lv, -3) for lv in _levels(tel.sent_frames)]


async def test_unrecorded_acknowledgment_is_skipped_not_synthesized_on_the_spot():
    tts, tel = LevelTTS(delay=0.2), Pcm16Telephony()
    session = _session(tts, tel, AckEngine())
    session._acknowledge()
    assert session._speaking_task is None  # nothing played this turn
    await asyncio.sleep(1.2)  # ...but it gets recorded in the background for next time
    assert acknowledgments.cached(tts, "Okay.") is not None
    await session.close()
