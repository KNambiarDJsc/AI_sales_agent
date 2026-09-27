"""Audio codec/resample helpers (Section 16).

Typical telephony path: mu-law 8kHz -> PCM16 8kHz -> resample -> PCM16 16kHz -> STT.
The reverse path (TTS at 24kHz -> mu-law 8kHz) is used to send audio back down the
telephony leg. This module never duplicates samples to fake a higher rate — it always
goes through a real resampler (audioop.ratecv), per the architecture rule.
"""
from __future__ import annotations

import audioop  # noqa: F401 -- provided by audioop-lts on Python 3.13+, stdlib before that
from dataclasses import dataclass, field

TELEPHONY_SAMPLE_RATE_HZ = 8000
STT_SAMPLE_RATE_HZ = 16000


@dataclass
class ResampleState:
    """Wraps audioop.ratecv's opaque state so a stream can be resampled chunk by chunk
    without clicks/pops at chunk boundaries."""

    state: object | None = None


def mulaw_to_pcm16(mulaw_bytes: bytes) -> bytes:
    return audioop.ulaw2lin(mulaw_bytes, 2)


def pcm16_to_mulaw(pcm16_bytes: bytes) -> bytes:
    return audioop.lin2ulaw(pcm16_bytes, 2)


def resample_pcm16(
    pcm16_bytes: bytes,
    from_rate: int,
    to_rate: int,
    resample_state: ResampleState,
    channels: int = 1,
) -> bytes:
    if from_rate == to_rate:
        return pcm16_bytes
    converted, new_state = audioop.ratecv(pcm16_bytes, 2, channels, from_rate, to_rate, resample_state.state)
    resample_state.state = new_state
    return converted


def telephony_frame_to_stt_pcm16(mulaw_bytes: bytes, resample_state: ResampleState) -> bytes:
    """mu-law 8kHz (from the telephony provider) -> PCM16 16kHz (for OpenAI STT)."""
    pcm16_8k = mulaw_to_pcm16(mulaw_bytes)
    return resample_pcm16(pcm16_8k, TELEPHONY_SAMPLE_RATE_HZ, STT_SAMPLE_RATE_HZ, resample_state)


def tts_pcm16_to_telephony_frame(pcm16_bytes: bytes, tts_sample_rate: int, resample_state: ResampleState) -> bytes:
    """PCM16 at the TTS provider's native rate -> mu-law 8kHz for the telephony leg."""
    pcm16_8k = resample_pcm16(pcm16_bytes, tts_sample_rate, TELEPHONY_SAMPLE_RATE_HZ, resample_state)
    return pcm16_to_mulaw(pcm16_8k)


def outbound_frame_bytes(frame_ms: int, sample_rate_hz: int = TELEPHONY_SAMPLE_RATE_HZ) -> int:
    """mu-law is 1 byte/sample, so this is just samples-per-frame."""
    return int(sample_rate_hz * frame_ms / 1000)


@dataclass
class FrameChunker:
    """Re-chunks a stream of outbound mu-law audio into fixed-size frames (~20ms by
    convention, `config.settings.outbound_frame_ms`) regardless of whatever chunk
    sizes the TTS provider happened to hand back, buffering any leftover partial frame
    across calls.

    This exists for two concrete reasons, not just protocol tidiness:
    1. Twilio/Exotel's media-stream protocols are documented and tuned around ~20ms
       payloads per message; sending arbitrarily large frames (a raw TTS chunk can be
       many times that) risks choppier playback than the provider expects.
    2. Barge-in latency (Section 18): `voice/session/session.py`'s speaking loop can
       only react to cancellation between two consecutive `await telephony.send_audio`
       calls. Larger frames mean fewer, further-apart await points, which means a
       bigger worst-case delay between "customer started talking" and "we actually
       stop sending audio." Fixed ~20ms frames bound that worst case to ~20ms of
       audio-equivalent time instead of however large the provider's internal chunk
       happens to be.
    """

    frame_bytes: int
    _buffer: bytearray = field(default_factory=bytearray)

    def push(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames = []
        while len(self._buffer) >= self.frame_bytes:
            frames.append(bytes(self._buffer[: self.frame_bytes]))
            del self._buffer[: self.frame_bytes]
        return frames

    def flush(self) -> bytes | None:
        """Call once at the end of an utterance: better to send a short final frame
        than to silently drop trailing audio still sitting in the buffer."""
        if not self._buffer:
            return None
        remainder = bytes(self._buffer)
        self._buffer.clear()
        return remainder
