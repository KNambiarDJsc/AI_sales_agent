"""The greeting is recorded while the phone rings and played the instant the lead
picks up (live Vobiz call: ~1.8 s of silence after pickup while it was synthesized)."""
import asyncio

import pytest

from tests.unit.test_voice_acknowledgment import LevelTTS, Pcm16Telephony
from tests.unit.test_voice_session_speak import FakeSTTProvider, _identity
from voice.session import acknowledgments, opening
from voice.session.session import VoiceSession

pytestmark = pytest.mark.asyncio


async def test_opening_is_recorded_during_ringing_and_played_without_synthesis(monkeypatch):
    tts = LevelTTS()
    monkeypatch.setattr(opening, "get_tts_provider", lambda: tts)
    opening.prerecord_opening("seller-flow", opening.lead_fields("Naman", "Demo Business", {}))
    await asyncio.gather(*opening._tasks)
    text = "Hello, am I speaking with Naman from Demo Business?"
    assert acknowledgments.cached(tts, text) is not None

    synthesized = []
    original = tts.synthesize_stream

    def spy(t):
        synthesized.append(t)
        return original(t)
    monkeypatch.setattr(tts, "synthesize_stream", spy)
    tel = Pcm16Telephony()
    s = VoiceSession(identity=_identity(), telephony=tel, stt_provider=FakeSTTProvider(), tts_provider=tts,
                     engine=None, session_factory=None)
    s._start_speaking(text)
    await s._speaking_task
    assert tel.sent_frames and synthesized == []


async def test_missing_lead_fields_record_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(opening.acknowledgments, "prerecord_one_off", lambda *a: calls.append(a) or asyncio.sleep(0))
    opening.prerecord_opening("seller-flow", opening.lead_fields("", "Demo Business", {}))
    assert calls == []
