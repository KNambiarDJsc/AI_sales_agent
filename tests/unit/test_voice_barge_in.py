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


@pytest.fixture(autouse=True)
async def _stop_turn_workers(monkeypatch):
    sessions = []
    original = VoiceSession._queue_turn

    def tracking(self, talked_over):
        sessions.append(self)
        original(self, talked_over)
    monkeypatch.setattr(VoiceSession, "_queue_turn", tracking)
    yield
    for s in sessions:
        if s._turn_worker is not None:
            s._turn_worker.cancel()


async def _agent_talking(session):
    session._speaking_task = asyncio.create_task(asyncio.sleep(30))


async def test_short_backchannel_while_agent_talks_neither_interrupts_nor_gets_a_reply():
    tel = Pcm16Telephony()
    s = _session(tel)
    s._stt_stream = FinalOnce("haan")
    replied = []
    s._on_turn_ended = lambda text: replied.append(text) or asyncio.sleep(0)
    await _agent_talking(s)
    await _feed(s, [(True, 300 // FRAME_MS), (False, 1000 // FRAME_MS)])  # 0.3 s "haan", then silence
    await s._turns_done()
    assert not tel.cleared and not s._speaking_task.done()  # agent kept talking
    assert replied == [] and s._stt_stream.calls == 1  # transcript taken out, no reply
    s._speaking_task.cancel()


async def test_a_short_real_answer_over_the_agent_still_gets_a_reply():
    # Live Vobiz call: a quick "no" said while the agent was finishing its sentence was
    # thrown away as a backchannel, and the agent sat waiting for an answer already given.
    tel = Pcm16Telephony()
    s = _session(tel)
    s._stt_stream = FinalOnce("No, not interested")
    replied = []
    s._on_turn_ended = lambda text: replied.append(text) or asyncio.sleep(0)
    await _agent_talking(s)
    await _feed(s, [(True, 300 // FRAME_MS), (False, 1000 // FRAME_MS)])
    await s._turns_done()
    assert not tel.cleared  # didn't cut the agent off...
    assert replied == ["No, not interested"]  # ...but the answer is answered
    s._speaking_task.cancel()


async def test_listening_never_waits_for_the_reply():
    # Live Vobiz call: handle_inbound_audio awaited the whole LLM turn, so the call
    # stopped listening while the agent thought — barge-in and the next turn stalled.
    tel = Pcm16Telephony()
    s = _session(tel)
    s._stt_stream = FinalOnce("tell me more")
    release = asyncio.Event()
    replied = []

    async def slow_turn(text):
        replied.append(text)
        await release.wait()
    s._on_turn_ended = slow_turn
    await asyncio.wait_for(_feed(s, [(True, 400 // FRAME_MS), (False, 800 // FRAME_MS)]), 1)
    await asyncio.sleep(0.01)
    assert replied == ["tell me more"]  # the turn is being answered...
    await asyncio.wait_for(_feed(s, [(True, 400 // FRAME_MS), (False, 800 // FRAME_MS)]), 1)  # ...and we still listen
    release.set()
    await s._turns_done()
    assert replied == ["tell me more", "tell me more"]


async def test_sustained_speech_interrupts_and_gets_a_reply():
    tel = Pcm16Telephony()
    s = _session(tel)
    s._stt_stream = FinalOnce("wait, I have a question")
    replied = []

    async def on_turn(text):
        replied.append(1)
    s._on_turn_ended = on_turn
    await _agent_talking(s)
    await _feed(s, [(True, 900 // FRAME_MS), (False, 1000 // FRAME_MS)])
    await s._turns_done()
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


async def test_vobiz_audio_goes_out_at_the_tts_rate_untouched():
    # Live Vobiz call: 24 kHz TTS squeezed to 8 kHz by a filterless resampler sounded
    # harsh and unclear. Vobiz takes 24 kHz L16, so the samples go out as they are.
    tel = Pcm16Telephony()
    tel.outbound_sample_rate_hz = 24000
    tel.outbound_frame_ms = 40
    tel.outbound_lead_ms = 10_000
    s = _session(tel)
    pcm = bytes(range(256)) * 30  # 7680 bytes = 160 ms at 24 kHz
    await s._play(_one(pcm))
    assert b"".join(tel.sent_frames) == pcm
    assert {len(f) for f in tel.sent_frames} == {1920}  # 40 ms at 24 kHz, 16-bit


async def _one(pcm):
    yield pcm
