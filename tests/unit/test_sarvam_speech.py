"""Sarvam STT/TTS adapters against local fakes of the documented protocol, their
fallback to OpenAI, the speakable-text filter and the uninterruptible greeting."""
import asyncio
import base64
import json
import struct
import urllib.parse

import httpx
import pytest
import websockets

from config.settings import get_settings
from speech.stt import sarvam as sarvam_stt
from speech.tts import sarvam as sarvam_tts
from speech.tts import sarvam_fallback
from speech.tts.base import TTSProvider
from tests.unit.test_voice_acknowledgment import LevelTTS, Pcm16Telephony
from tests.unit.test_voice_barge_in import FRAME_MS, _feed, _session
from voice.session.speech_pipeline import phrases_from, speakable, synthesize_phrases

pytestmark = pytest.mark.asyncio


# --- STT -------------------------------------------------------------------------------

async def _fake_sarvam_stt(transcripts: list[str], seen: dict):
    """A local server speaking the documented /speech-to-text/ws protocol: collects the
    audio, answers each flush with the next transcript."""
    async def handler(ws):
        seen["path"] = ws.request.path
        seen["key"] = ws.request.headers.get("Api-Subscription-Key")
        seen["audio"] = []
        async for raw in ws:
            msg = json.loads(raw)
            if "audio" in msg:
                seen["audio"].append(msg["audio"])
            elif msg.get("type") == "flush":
                text = transcripts.pop(0) if transcripts else ""
                await ws.send(json.dumps({"type": "data", "data": {
                    "request_id": "r", "transcript": text, "timestamps": None, "diarized_transcript": None,
                    "language_code": "hi-IN", "language_probability": None,
                    "metrics": {"audio_duration": 1.0, "processing_latency": 0.1}}}))
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


async def test_sarvam_stt_speaks_the_documented_protocol():
    seen: dict = {}
    server, base = await _fake_sarvam_stt(["हाँ जी, मैं Naman बोल रहा हूँ"], seen)
    try:
        url = sarvam_stt.stt_url(base, "saaras:v4", "codemix", "hi-IN")
        stream = sarvam_stt.SarvamSTTStream("test-key", url)
        await stream.connect()
        await stream.send_audio(b"\x01\x00" * 160)
        result = await stream.receive_final()
        await stream.close()
    finally:
        server.close()
    assert result.text == "हाँ जी, मैं Naman बोल रहा हूँ"
    path, query = seen["path"].split("?")
    params = dict(urllib.parse.parse_qsl(query))
    assert path == "/speech-to-text/ws" and seen["key"] == "test-key"
    assert params == {"language-code": "hi-IN", "model": "saaras:v4", "mode": "codemix",
                      "sample_rate": "16000", "flush_signal": "true"}
    audio = seen["audio"][0]
    assert audio["sample_rate"] == "16000" and audio["encoding"] == "audio/wav"
    assert base64.b64decode(audio["data"]) == b"\x01\x00" * 160


async def test_sarvam_stt_returns_segments_its_own_vad_sent_before_the_flush():
    stream = sarvam_stt.SarvamSTTStream("k", "ws://unused")

    class _WS:
        async def send(self, _):
            pass
    stream._ws = _WS()
    stream._segments = ["नहीं,", "अभी नहीं"]
    result = await asyncio.wait_for(stream.receive_final(), timeout=2)
    assert result.text == "नहीं, अभी नहीं"
    assert stream._segments == []


async def test_sarvam_stt_falls_back_to_openai_when_it_cannot_connect(monkeypatch):
    # A dead transcriber would leave the agent deaf for the whole call.
    from speech.stt import openai as openai_stt

    class FakeOpenAIStream:
        def __init__(self):
            self.audio = b""

        async def send_audio(self, chunk):
            self.audio += chunk

        async def receive_final(self):
            return sarvam_stt.TranscriptResult(text="okay", is_final=True)

        async def close(self):
            pass

    fake = FakeOpenAIStream()

    async def start_stream(self):
        return fake
    monkeypatch.setattr(openai_stt.OpenAISTTProvider, "__init__", lambda self: None)
    monkeypatch.setattr(openai_stt.OpenAISTTProvider, "start_stream", start_stream)
    provider = sarvam_stt.SarvamWithFallbackSTTProvider()
    provider._url = "ws://127.0.0.1:9/speech-to-text/ws"  # nothing listens there
    stream = await provider.start_stream()
    await stream.send_audio(b"\x02\x00" * 10)
    assert (await stream.receive_final()).text == "okay"
    assert fake.audio == b"\x02\x00" * 10


# --- TTS -------------------------------------------------------------------------------

def _wav(pcm: bytes, rate=24000) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def _mock_sarvam(monkeypatch, handler):
    client = httpx.AsyncClient(base_url="https://api.sarvam.ai", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(sarvam_tts, "_client", lambda: client)


async def test_sarvam_tts_request_and_wav_header_is_stripped(monkeypatch):
    requests = []
    pcm = bytes(range(256)) * 10

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=_wav(pcm))
    _mock_sarvam(monkeypatch, handler)
    tts = sarvam_tts.SarvamTTSProvider()
    audio = b"".join([c async for c in tts.synthesize_stream("नमस्ते, आपका order confirm हो गया है।")])
    assert audio == pcm
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/text-to-speech/stream"
    assert requests[0].headers["api-subscription-key"] == get_settings().sarvam_api_key
    s = get_settings()
    assert body["language_code"] == "hi-IN" and body["speaker"] == s.sarvam_tts_speaker
    assert body["model"] == s.sarvam_tts_model and body["output_audio_codec"] == "linear16"
    assert body["speech_sample_rate"] == 24000


async def test_sarvam_tts_reads_an_english_reply_with_the_english_persona(monkeypatch):
    requests = []
    _mock_sarvam(monkeypatch, lambda r: requests.append(r) or httpx.Response(200, content=b"\x00\x01" * 50))
    tts = sarvam_tts.SarvamTTSProvider()
    audio = b"".join([c async for c in tts.synthesize_stream("Sure, our specialist will call you tomorrow.")])
    assert audio == b"\x00\x01" * 50  # no header: passed through as is
    body = json.loads(requests[0].content)
    assert body["language_code"] == "en-IN" and body["speaker"] == get_settings().sarvam_tts_speaker_en


async def test_sarvam_tts_failure_falls_back_to_openai_before_any_audio(monkeypatch):
    monkeypatch.setattr(sarvam_fallback, "_sarvam_failed_at", None)
    _mock_sarvam(monkeypatch, lambda r: httpx.Response(403, json={"error": {"message": "no credits"}}))

    class FakeOpenAI(TTSProvider):
        sample_rate_hz = 24000

        async def synthesize_stream(self, text):
            yield b"\x05\x00" * 4

        async def cancel(self):
            pass

        async def close(self):
            pass
    tts = sarvam_fallback.SarvamFallbackTTSProvider(primary=sarvam_tts.SarvamTTSProvider(), secondary=FakeOpenAI())
    audio = b"".join([c async for c in tts.synthesize_stream("जी।")])
    assert audio == b"\x05\x00" * 4 and tts.backend == "openai"
    # The next phrase doesn't wait on another failing Sarvam request.
    calls = []
    _mock_sarvam(monkeypatch, lambda r: calls.append(r) or httpx.Response(403))
    assert b"".join([c async for c in tts.synthesize_stream("अच्छा।")]) == b"\x05\x00" * 4
    assert calls == []


def test_wav_header_split_across_chunks():
    header_and_some = _wav(b"\x01\x02\x03\x04")
    assert sarvam_tts.strip_wav_header(header_and_some[:20]) == (b"", False)
    assert sarvam_tts.strip_wav_header(header_and_some) == (b"\x01\x02\x03\x04", True)


# --- What reaches the voice -----------------------------------------------------------

def test_foreign_script_is_never_spoken():
    # Live Hindi call: "मैं समझ गई ... आप安心 रहिए".
    assert speakable("आप安心 रहिए, ₹500 — okay?") == "आप रहिए, ₹500 — okay?"
    assert speakable("安心") == ""


async def test_a_phrase_with_nothing_speakable_is_skipped():
    tts = LevelTTS()
    audio = [c async for c in synthesize_phrases(tts, phrases_from(["安心", "जी।"]))]
    assert audio  # the second phrase still plays


# --- The greeting -----------------------------------------------------------------------

async def test_hello_over_the_greeting_does_not_cut_it_off():
    # Live Vobiz call, 2026-10-28: "hello? hello?" on pickup stopped the greeting and
    # the customer asked why there was no voice.
    tel = Pcm16Telephony()
    s = _session(tel)
    s._engine = type("E", (), {"scripted_opening": lambda self: "नमस्ते", "all_acknowledgments": lambda self: []})()
    s._start_speaking = lambda text: setattr(s, "_speaking_task", asyncio.create_task(asyncio.sleep(30)))
    s._queue_turn = lambda talked_over: None
    await s.speak_opening_line()
    await _feed(s, [(True, 1500 // FRAME_MS)])  # 1.5 s of "hello? hello?"
    assert not tel.cleared and not s._speaking_task.done()
    s._speaking_task.cancel()


async def test_talking_over_a_normal_reply_still_interrupts_it():
    tel = Pcm16Telephony()
    s = _session(tel)
    s._queue_turn = lambda talked_over: None
    s._speaking_task = asyncio.create_task(asyncio.sleep(30))
    await _feed(s, [(True, 1500 // FRAME_MS)])
    assert tel.cleared
