"""Barge-in needs sustained speech; short sounds while the agent talks are ignored
(first live Vobiz call: every "haan", cough and echo cut the agent off mid-sentence
and queued another reply — heard as several agents talking and sentences missing
their middle)."""
import asyncio

import pytest

from speech.stt.base import TranscriptResult
from tests.unit.test_voice_acknowledgment import LevelTTS, Pcm16Telephony
from tests.unit.test_voice_session_speak import FakeSTTProvider, _identity
from voice.session.session import VoiceSession
from voice.session.speech_pipeline import phrases_from

pytestmark = pytest.mark.asyncio
FRAME_MS = 20


class FinalOnce:
    def __init__(self, text):
        self.text, self.calls = text, 0

    async def receive_final(self):
        self.calls += 1
        return TranscriptResult(text=self.text, is_final=True)


def _session(tel):
    s = VoiceSession(identity=_identity(), telephony=tel, stt_provider=FakeSTTProvider(), tts_provider=LevelTTS(),
                     engine=None, session_factory=None)
    s._settings = s._settings.model_copy(update={"barge_in_grace_ms": 0})  # this session only
    return s


async def _feed(session, pattern):
    """pattern: list of (is_speech, frames)."""
    for is_speech, frames in pattern:
        for _ in range(frames):
            session._vad.is_speech = lambda *_a, _v=is_speech, **_k: _v
            await session._process_vad_frame(b"\x00" * 640)


async def _agent_talking(session):
    session._speaking_task = asyncio.create_task(asyncio.sleep(30))


async def test_short_backchannel_while_agent_talks_neither_interrupts_nor_gets_a_reply():
    tel = Pcm16Telephony()
    s = _session(tel)
    s._stt_stream = FinalOnce("haan")
    replied = []
    s._on_turn_ended = lambda: replied.append(1) or asyncio.sleep(0)
    await _agent_talking(s)
    await _feed(s, [(True, 300 // FRAME_MS), (False, 1000 // FRAME_MS)])  # 0.3 s "haan", then silence
    assert not tel.cleared and not s._speaking_task.done()  # agent kept talking
    assert replied == [] and s._stt_stream.calls == 1  # transcript taken out, no reply
    s._speaking_task.cancel()


async def test_sustained_speech_interrupts_and_gets_a_reply():
    tel = Pcm16Telephony()
    s = _session(tel)
    s._stt_stream = FinalOnce("wait, I have a question")
    replied = []

    async def on_turn():
        replied.append(1)
    s._on_turn_ended = on_turn
    await _agent_talking(s)
    await _feed(s, [(True, 900 // FRAME_MS), (False, 1000 // FRAME_MS)])
    assert tel.cleared and s._speaking_task is None  # cut off after ~0.6 s of speech
    assert replied == [1]


async def test_vobiz_audio_runs_ahead_of_real_time_by_the_lead():
    tel = Pcm16Telephony()
    tel.outbound_lead_ms = 400
    s = _session(tel)
    t0 = asyncio.get_event_loop().time()
    await s._speak_phrases(phrases_from(["Hello there."]))  # 0.4 s of audio from LevelTTS
    elapsed = asyncio.get_event_loop().time() - t0
    assert tel.sent_frames and elapsed < 0.2  # all of it fits in the lead: no waiting
