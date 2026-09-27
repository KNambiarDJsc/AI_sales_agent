"""Audio codec/resample helpers (Section 16).

Typical telephony path: mu-law 8kHz -> PCM16 8kHz -> resample -> PCM16 16kHz -> STT.
The reverse path (TTS at 24kHz -> mu-law 8kHz) is used to send audio back down the
telephony leg. This module never duplicates samples to fake a higher rate — it always
goes through a real resampler (audioop.ratecv), per the architecture rule.
"""
from __future__ import annotations

import audioop  # noqa: F401 -- provided by audioop-lts on Python 3.13+, stdlib before that
from dataclasses import dataclass

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
