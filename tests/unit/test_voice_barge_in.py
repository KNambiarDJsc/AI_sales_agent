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


class LimitEngine:
    def __init__(self, limit):
        self.limit = limit

    def call_limit_seconds(self):
        return self.limit

    def time_up_line(self):
        return "Sorry, I have to wrap up now."


class HangupTelephony(Pcm16Telephony):
    def __init__(self):
        super().__init__()
        self.hung_up = []

    async def hangup_call(self, provider_call_id):
        self.hung_up.append(provider_call_id)


async def test_silence_hangs_up_but_speech_or_the_agent_talking_keeps_the_call():
    # Live Vobiz call, 2026-10-22: the customer hung up, Vobiz kept the call open and the
    # console showed it live until it was ended by hand. Nobody talking for
    # silence_hangup_seconds now ends the call from our side.
    tel = HangupTelephony()
    s = _session(tel)
    s._engine = LimitEngine(limit=600)
    s._settings = s._settings.model_copy(update={"silence_hangup_seconds": 2})
    loop = asyncio.get_event_loop()
    s._last_activity = s._started_at = loop.time()
    watch = asyncio.create_task(s._watch_call())
    await asyncio.sleep(1.2)
    await _feed(s, [(True, 5)])  # the customer says something
    await asyncio.sleep(1.2)
    assert tel.hung_up == [] and not s.ended
    await _agent_talking(s)  # agent talking is activity too
    await asyncio.sleep(2.5)
    assert tel.hung_up == []
    s._speaking_task.cancel()
    await asyncio.wait_for(watch, 5)  # then silence: hung up and closed
    assert len(tel.hung_up) == 1 and s.ended


async def test_time_limit_says_goodbye_then_hangs_up_and_takes_no_more_turns():
    # Owner's rule (2026-10-22): no call runs past 2 minutes.
    tel = HangupTelephony()
    s = _session(tel)
    s._engine = LimitEngine(limit=120)
    said = []
    s._start_speaking = lambda text: said.append(text) or setattr(s, "_speaking_task", asyncio.create_task(asyncio.sleep(0.1)))
    loop = asyncio.get_event_loop()
    s._last_activity = loop.time()
    s._started_at = loop.time() - 111  # 111 s in: past the 120 - 10 s wrap-up point
    await asyncio.wait_for(s._watch_call(), 5)
    assert said == ["Sorry, I have to wrap up now."]
    assert len(tel.hung_up) == 1 and s.ended
    await s._run_agent_turn("one more thing")  # ignored: no new reply after the goodbye
