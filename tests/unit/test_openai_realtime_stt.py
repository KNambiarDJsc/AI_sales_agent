"""Tests for speech/stt/openai_realtime.py against a fake websocket connection — no
network needed. The event shapes used here are exactly what was observed connecting to
the real API (see STATUS.md's "Realtime STT migration" section), not guessed.
"""
import asyncio
import json

import pytest

import speech.stt.openai_realtime as rt_module
from speech.stt.openai_realtime import OpenAIRealtimeSTTStream

pytestmark = pytest.mark.asyncio


class FakeWSConnection:
    """Replays a canned list of server messages via both `.recv()` (used for the two
    handshake messages) and async iteration (used by the read loop), and records
    everything sent via `.send()`."""

    def __init__(self, incoming: list[str]):
        self._incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        if not self._incoming:
            raise AssertionError("FakeWSConnection.recv() called with nothing queued")
        return self._incoming.pop(0)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            # A real websocket doesn't "end" just because nothing new has arrived
            # yet — it only ends on actual close/disconnect. Hang until cancelled
            # (every test's `finally: await stream.close()` does this), so a test
            # that runs out of canned messages genuinely exercises "still connected,
            # nothing to report" rather than "connection closed."
            await asyncio.Event().wait()
        return self._incoming.pop(0)


def _session_created() -> str:
    return json.dumps({"type": "session.created", "session": {"type": "transcription"}})


def _session_updated() -> str:
    return json.dumps({"type": "session.updated", "session": {"type": "transcription"}})


def _delta(text: str) -> str:
    return json.dumps({"type": "conversation.item.input_audio_transcription.delta", "item_id": "item_1", "delta": text})


def _completed(transcript: str) -> str:
    return json.dumps(
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "item_1", "transcript": transcript}
    )


async def _connected_stream(monkeypatch, incoming: list[str]) -> tuple[OpenAIRealtimeSTTStream, FakeWSConnection]:
    fake_ws = FakeWSConnection(incoming)

    async def fake_connect(*args, **kwargs):
        return fake_ws

    monkeypatch.setattr(rt_module.websockets, "connect", fake_connect)
    stream = OpenAIRealtimeSTTStream(api_key="sk-fake", model="gpt-4o-transcribe")
    await stream.connect()
    return stream, fake_ws


async def test_connect_sends_correct_session_update_and_disables_server_vad(monkeypatch):
    stream, fake_ws = await _connected_stream(monkeypatch, [_session_created(), _session_updated()])
    try:
        assert len(fake_ws.sent) == 1
        sent = json.loads(fake_ws.sent[0])
        assert sent["type"] == "session.update"
        audio_input = sent["session"]["audio"]["input"]
        assert audio_input["format"]["rate"] == rt_module.REALTIME_STT_SAMPLE_RATE_HZ
        assert audio_input["format"]["rate"] >= 24000  # confirmed hard floor
        assert audio_input["transcription"]["model"] == "gpt-4o-transcribe"
        assert audio_input["turn_detection"] is None  # we do our own endpointing
    finally:
        await stream.close()


async def test_connect_raises_on_rejected_session_update(monkeypatch):
    error_ack = json.dumps({"type": "error", "error": {"message": "nope"}})
    fake_ws = FakeWSConnection([_session_created(), error_ack])

    async def fake_connect(*args, **kwargs):
        return fake_ws

    monkeypatch.setattr(rt_module.websockets, "connect", fake_connect)
    stream = OpenAIRealtimeSTTStream(api_key="sk-fake", model="gpt-4o-transcribe")
    with pytest.raises(RuntimeError, match="rejected"):
        await stream.connect()
    assert fake_ws.closed  # cleaned up rather than leaking a half-open socket


async def test_send_audio_sends_base64_encoded_payload(monkeypatch):
    stream, fake_ws = await _connected_stream(monkeypatch, [_session_created(), _session_updated()])
    try:
        await stream.send_audio(b"\x01\x02\x03\x04")
        sent = json.loads(fake_ws.sent[-1])
        assert sent["type"] == "input_audio_buffer.append"
        import base64

        assert base64.b64decode(sent["audio"]) == b"\x01\x02\x03\x04"
    finally:
        await stream.close()


async def test_partial_transcripts_accumulate_across_delta_events(monkeypatch):
    # No `completed` event queued — nothing resets partial_text, so this deterministically
    # observes the mid-utterance accumulated state (unlike mixing in a completion, which
    # the fake's instant resolution would race ahead to before any check could run).
    incoming = [_session_created(), _session_updated(), _delta("Hello"), _delta(" there")]
    stream, fake_ws = await _connected_stream(monkeypatch, incoming)
    try:
        for _ in range(5):
            await asyncio.sleep(0)  # let the reader loop drain both queued deltas

        partial = await stream.receive_partial()
        assert partial is not None
        assert partial.text == "Hello there"
        assert partial.is_final is False

        # Nothing new since last read.
        assert await stream.receive_partial() is None
    finally:
        await stream.close()


async def test_receive_final_returns_completed_transcript_and_sends_commit(monkeypatch):
    incoming = [_session_created(), _session_updated(), _delta("Hello there"), _completed("Hello there")]
    stream, fake_ws = await _connected_stream(monkeypatch, incoming)
    try:
        final = await stream.receive_final()
        assert final is not None
        assert final.text == "Hello there"
        assert final.is_final is True

        # receive_final() is what triggers the manual commit (turn_detection is off).
        commit_messages = [json.loads(m) for m in fake_ws.sent if json.loads(m)["type"] == "input_audio_buffer.commit"]
        assert len(commit_messages) == 1

        # partial_text was reset once the turn completed.
        assert await stream.receive_partial() is None
    finally:
        await stream.close()


async def test_receive_final_times_out_gracefully_if_nothing_arrives(monkeypatch):
    monkeypatch.setattr(rt_module, "FINAL_TRANSCRIPT_TIMEOUT_SECONDS", 0.05)
    stream, fake_ws = await _connected_stream(monkeypatch, [_session_created(), _session_updated()])
    try:
        result = await stream.receive_final()
        assert result is None  # timed out, not a crash
    finally:
        await stream.close()
