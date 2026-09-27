"""VoiceSession — the realtime media loop (Section 4A).

Owns: decoding inbound telephony audio, VAD/endpointing, buffering audio into the STT
stream, invoking the orchestrator on end-of-turn, streaming TTS audio back out, and
barge-in (Section 18). Knows nothing about SQL, campaigns, or qualification rules —
those live behind `ConversationEngine`, which this class treats as a black box that
takes text in and returns text + end_call out. This separation is what Section 4 means
by "keep the media layer independent of the agent reasoning layer."
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from orchestrator.engine import ConversationEngine
from speech.stt.base import STTProvider
from speech.tts.base import TTSProvider
from speech.tts.openai import OPENAI_TTS_SAMPLE_RATE_HZ
from telephony.base import TelephonyProvider
from voice.audio.processing import ResampleState, telephony_frame_to_stt_pcm16, tts_pcm16_to_telephony_frame
from voice.turn_taking.turn_taking import TurnEvent, TurnTaker
from voice.vad.vad import VoiceActivityDetector

logger = logging.getLogger(__name__)

VAD_FRAME_MS = 20
VAD_FRAME_BYTES_PCM16_16K = int(16000 * (VAD_FRAME_MS / 1000.0)) * 2  # 640 bytes


@dataclass
class SessionIdentity:
    provider_call_id: str
    tenant_id: UUID
    campaign_id: UUID
    lead_id: UUID
    conversation_id: UUID


class VoiceSession:
    def __init__(
        self,
        identity: SessionIdentity,
        telephony: TelephonyProvider,
        stt_provider: STTProvider,
        tts_provider: TTSProvider,
        engine: ConversationEngine,
        session_factory,
    ):
        self._identity = identity
        self._telephony = telephony
        self._tts_provider = tts_provider
        self._engine = engine
        self._session_factory = session_factory

        self._vad = VoiceActivityDetector(sample_rate_hz=16000)
        self._turn_taker = TurnTaker()
        self._resample_in = ResampleState()
        self._resample_out = ResampleState()
        self._pending_pcm16 = bytearray()  # accumulates until we have a full VAD frame

        self._stt_provider = stt_provider
        self._stt_stream = None  # created lazily in `start()`

        self._speaking_task: asyncio.Task | None = None
        self._ended = False

    async def start(self) -> None:
        self._stt_stream = await self._stt_provider.start_stream()

    async def speak_opening_line(self) -> None:
        """Kick off the call: the agent speaks first (Section 32's vertical slice —
        greet, identify the business, ask permission). Reuses the normal turn path
        with empty customer text so the same validation/state-machine rules apply to
        the opening line as to every other turn."""
        await self._run_agent_turn("")

    async def handle_inbound_audio(self, mulaw_chunk: bytes) -> None:
        """Called by the WebSocket handler for every inbound media frame from the
        telephony provider (mu-law 8kHz)."""
        if self._ended or self._stt_stream is None:
            return

        pcm16_16k = telephony_frame_to_stt_pcm16(mulaw_chunk, self._resample_in)
        self._pending_pcm16.extend(pcm16_16k)

        while len(self._pending_pcm16) >= VAD_FRAME_BYTES_PCM16_16K:
            frame = bytes(self._pending_pcm16[:VAD_FRAME_BYTES_PCM16_16K])
            del self._pending_pcm16[:VAD_FRAME_BYTES_PCM16_16K]
            await self._process_vad_frame(frame)

    async def _process_vad_frame(self, pcm16_frame: bytes) -> None:
        is_speech = self._vad.is_speech(pcm16_frame, frame_ms=VAD_FRAME_MS)
        await self._stt_stream.send_audio(pcm16_frame)
        event = self._turn_taker.feed(is_speech, VAD_FRAME_MS)

        if event == TurnEvent.SPEECH_STARTED and self._speaking_task is not None and not self._speaking_task.done():
            await self._handle_barge_in()

        if event in (TurnEvent.TURN_ENDED, TurnEvent.MAX_TURN_REACHED):
            await self._on_turn_ended()

    async def _handle_barge_in(self) -> None:
        """Section 18: stop TTS, clear the provider's buffered audio, cancel the
        in-flight speaking task. The next turn (customer's interruption) is handled
        normally once endpointing detects their turn has ended."""
        logger.info("barge_in", extra={"call_id": self._identity.provider_call_id})
        await self._tts_provider.cancel()
        await self._telephony.clear_audio(self._identity.provider_call_id)
        if self._speaking_task is not None:
            self._speaking_task.cancel()
            self._speaking_task = None

    async def _on_turn_ended(self) -> None:
        transcript = await self._stt_stream.receive_final()
        customer_text = transcript.text if transcript else ""
        if not customer_text.strip():
            return  # nothing intelligible captured this turn; wait for more audio
        await self._run_agent_turn(customer_text)

    async def _run_agent_turn(self, customer_text: str) -> None:
        turn_result = await self._engine.run_turn(
            customer_text,
            session_factory=self._session_factory,
            tenant_id=self._identity.tenant_id,
            campaign_id=self._identity.campaign_id,
            lead_id=self._identity.lead_id,
            conversation_id=self._identity.conversation_id,
        )

        self._speaking_task = asyncio.create_task(self._speak(turn_result.speech))

        if turn_result.end_call:
            await self._speaking_task
            await self._telephony.hangup_call(self._identity.provider_call_id)
            await self.close()

    async def _speak(self, text: str) -> None:
        try:
            async for pcm16_chunk in self._tts_provider.synthesize_stream(text):
                telephony_frame = tts_pcm16_to_telephony_frame(
                    pcm16_chunk, OPENAI_TTS_SAMPLE_RATE_HZ, self._resample_out
                )
                await self._telephony.send_audio(self._identity.provider_call_id, telephony_frame)
        except asyncio.CancelledError:
            logger.info("tts_playback_cancelled", extra={"call_id": self._identity.provider_call_id})
            raise

    async def close(self) -> None:
        self._ended = True
        if self._speaking_task is not None and not self._speaking_task.done():
            self._speaking_task.cancel()
        if self._stt_stream is not None:
            await self._stt_stream.close()
        await self._tts_provider.close()
