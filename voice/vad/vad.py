"""VAD: "is speech occurring right now?" — deliberately separate from endpointing
("has the customer finished their turn?"), which lives in voice/turn_taking (Section
17). Uses WebRTC VAD (provider-agnostic, works on any 8/16kHz PCM16 frame) rather than
a provider-native VAD, so this doesn't depend on Twilio/Exotel/OpenAI specifics.
"""
from __future__ import annotations

import webrtcvad

# webrtcvad requires frames of exactly 10, 20, or 30 ms at 8/16/32/48kHz.
SUPPORTED_FRAME_MS = (10, 20, 30)


class VoiceActivityDetector:
    def __init__(self, aggressiveness: int = 2, sample_rate_hz: int = 16000):
        if sample_rate_hz not in (8000, 16000, 32000, 48000):
            raise ValueError(f"Unsupported sample rate for VAD: {sample_rate_hz}")
        self._vad = webrtcvad.Vad(aggressiveness)
        self._sample_rate = sample_rate_hz

    def is_speech(self, pcm16_frame: bytes, frame_ms: int = 20) -> bool:
        if frame_ms not in SUPPORTED_FRAME_MS:
            raise ValueError(f"Frame duration must be one of {SUPPORTED_FRAME_MS}ms, got {frame_ms}")
        expected_bytes = int(self._sample_rate * (frame_ms / 1000.0)) * 2  # PCM16 = 2 bytes/sample
        if len(pcm16_frame) != expected_bytes:
            raise ValueError(f"Expected {expected_bytes} bytes for a {frame_ms}ms frame, got {len(pcm16_frame)}")
        return self._vad.is_speech(pcm16_frame, self._sample_rate)
