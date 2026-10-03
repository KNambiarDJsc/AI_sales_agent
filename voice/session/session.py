"""VoiceSession — the realtime media loop (Section 4A).

Owns: decoding inbound telephony audio, VAD/endpointing, buffering audio into the STT
stream, invoking the orchestrator on end-of-turn, streaming TTS audio back out, and
barge-in (Section 18). Knows nothing about SQL, campaigns, or qualification rules —
those live behind `ConversationEngine`, which this class treats as a black box that
takes text in and, via a callback, hands back speech to play plus (once the turn fully
resolves) whether to hang up. This separation is what Section 4 means by "keep the
media layer independent of the agent reasoning layer."

See STATUS.md's "Latency & realtime" section for the reasoning behind:
- starting to speak from `on_speech_ready` (as soon as the engine knows what to say)
  instead of waiting for `run_turn()` to return (which also runs tool calls);
- pipelining TTS synthesis sentence-by-sentence;
- re-chunking outbound audio to fixed ~20ms frames;
- bounding the inbound audio buffer;
- timing out a stuck STT call instead of leaving the line silent forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from uuid import UUID

from config.settings import get_settings
from orchestrator.engine import ConversationEngine
from speech.stt.base import STTProvider
from speech.tts.base import TTSProvider
from speech.tts.openai import OPENAI_TTS_SAMPLE_RATE_HZ
from telephony.base import TelephonyProvider
from voice.audio.processing import (
    TELEPHONY_SAMPLE_RATE_HZ,
    VAD_SAMPLE_RATE_HZ,
    FrameChunker,
    ResampleState,
    mulaw_to_pcm16,
    outbound_frame_bytes,
    resample_pcm16,
    tts_pcm16_to_telephony_frame,
    tts_pcm16_to_telephony_pcm16,
)
from voice.session.sentence_split import split_into_speech_chunks
from voice.turn_taking.turn_taking import TurnEvent, TurnTaker
from voice.vad.vad import VoiceActivityDetector

logger = logging.getLogger(__name__)

VAD_FRAME_MS = 20
VAD_FRAME_BYTES_PCM16_16K = int(VAD_SAMPLE_RATE_HZ * (VAD_FRAME_MS / 1000.0)) * 2  # 640 bytes


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
        self._settings = get_settings()

        self._audio_encoding = telephony.audio_encoding  # "mulaw" (Twilio) | "pcm16" (Exotel)
        # aggressiveness=3 (most conservative about calling something "speech"): raised
        # from the default 2 after a live Exotel call showed real phone-line noise
        # being misread as speech far more than a clean browser-mic demo ever surfaced.
        self._vad = VoiceActivityDetector(sample_rate_hz=VAD_SAMPLE_RATE_HZ, aggressiveness=3)
        self._turn_taker = TurnTaker()
        self._resample_in = ResampleState()  # native 8k -> PCM16 16k, for VAD only
        self._resample_out = ResampleState()
        self._pending_pcm16 = bytearray()  # accumulates until we have a full VAD frame

        self._stt_provider = stt_provider
        self._stt_stream = None  # created lazily in `start()`
        self._consecutive_empty_turns = 0
        # STT gets its own independent resample chain at whatever rate the provider
        # declares (e.g. OpenAIRealtimeSTTProvider requires >=24kHz, the VAD stays at
        # 16kHz regardless) — both derived straight from the same mu-law source, not
        # one resampled from the other.
        self._stt_input_rate_hz = stt_provider.input_sample_rate_hz
        self._resample_stt = ResampleState()

        self._speaking_task: asyncio.Task | None = None
        self._first_frame_sent_at: float | None = None  # set in _speak(); see _process_vad_frame
        self._ended = False

    async def start(self) -> None:
        self._stt_stream = await self._stt_provider.start_stream()

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def conversation_state(self) -> str:
        return self._engine.current_state

    async def speak_opening_line(self) -> None:
        """Kick off the call: the agent speaks first (Section 32's vertical slice —
        greet, identify the business, ask permission). Reuses the normal turn path
        with empty customer text so the same validation/state-machine rules apply to
        the opening line as to every other turn."""
        await self._run_agent_turn("")

    async def handle_inbound_audio(self, audio_chunk: bytes) -> None:
        """Called by the WebSocket handler for every inbound media frame from the
        telephony provider, already in that provider's own encoding (mu-law 8kHz for
        Twilio, raw PCM16 8kHz for Exotel — see `TelephonyProvider.audio_encoding`)."""
        if self._ended or self._stt_stream is None:
            return

        pcm16_native = mulaw_to_pcm16(audio_chunk) if self._audio_encoding == "mulaw" else audio_chunk

        # Feed the STT provider directly and continuously at its own required rate —
        # not gated on VAD-frame chunking below, since the STT stream just needs a
        # correctly-ordered byte stream, not 20ms-aligned frames.
        stt_frame = resample_pcm16(pcm16_native, TELEPHONY_SAMPLE_RATE_HZ, self._stt_input_rate_hz, self._resample_stt)
        await self._stt_stream.send_audio(stt_frame)

        pcm16_16k = resample_pcm16(pcm16_native, TELEPHONY_SAMPLE_RATE_HZ, VAD_SAMPLE_RATE_HZ, self._resample_in)
        self._pending_pcm16.extend(pcm16_16k)

        # Backpressure guard (Section 16): if VAD/STT processing ever falls behind
        # real time, drop the oldest audio rather than growing this buffer (and the
        # latency of everything after it) without bound. In steady state this never
        # triggers — each frame's processing is cheap and keeps up with the ~20ms
        # cadence audio arrives at.
        overflow = len(self._pending_pcm16) - self._settings.max_pending_inbound_audio_bytes
        if overflow > 0:
            logger.warning(
                "inbound_audio_buffer_overflow",
                extra={"call_id": self._identity.provider_call_id, "dropped_bytes": overflow},
            )
            del self._pending_pcm16[:overflow]

        while len(self._pending_pcm16) >= VAD_FRAME_BYTES_PCM16_16K:
            frame = bytes(self._pending_pcm16[:VAD_FRAME_BYTES_PCM16_16K])
            del self._pending_pcm16[:VAD_FRAME_BYTES_PCM16_16K]
            await self._process_vad_frame(frame)

    async def _process_vad_frame(self, pcm16_frame: bytes) -> None:
        # STT already received this audio (at its own rate) from handle_inbound_audio;
        # this frame is 16kHz purely for VAD/endpointing.
        is_speech = self._vad.is_speech(pcm16_frame, frame_ms=VAD_FRAME_MS)
        event = self._turn_taker.feed(is_speech, VAD_FRAME_MS)

        if event == TurnEvent.SPEECH_STARTED and self._speaking_task is not None and not self._speaking_task.done():
            # Anchored to when the first frame of THIS utterance actually reached the
            # phone (self._first_frame_sent_at, set in _speak()) — not to when the
            # speaking task was merely created. Caught live: TTS synthesis itself can
            # take over a second before any audio exists, which is already longer than
            # the grace window, so anchoring to task-creation time protected nothing in
            # practice. If no frame has been sent yet, this can't be our own echo (there
            # is nothing yet to echo), so let it through rather than suppress it.
            if self._first_frame_sent_at is None:
                await self._handle_barge_in()
            else:
                grace_s = self._settings.barge_in_grace_ms / 1000.0
                elapsed = asyncio.get_event_loop().time() - self._first_frame_sent_at
                if elapsed >= grace_s:
                    await self._handle_barge_in()
                else:
                    logger.info(
                        "barge_in_suppressed_grace_window",
                        extra={"call_id": self._identity.provider_call_id, "elapsed_ms": int(elapsed * 1000)},
                    )

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
            task = self._speaking_task
            self._speaking_task = None
            task.cancel()
            # Caught live: cancelling without awaiting left _speak()'s cleanup (which
            # cancels its own producer tasks in a finally block) sometimes never
            # scheduled before this was the last reference, logging "Task was
            # destroyed but it is pending!" during a real multi-barge-in call. Awaiting
            # here guarantees that cleanup actually runs.
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _on_turn_ended(self) -> None:
        # Timing instrumentation (not guessing where per-turn latency goes): logs how
        # long the STT-final wait itself took, separate from the LLM/TTS time logged
        # in _run_agent_turn/_speak — added live while chasing a reported 4-5s gap.
        loop = asyncio.get_event_loop()
        stt_start = loop.time()
        try:
            transcript = await asyncio.wait_for(
                self._stt_stream.receive_final(), timeout=self._settings.stt_timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning("stt_timeout", extra={"call_id": self._identity.provider_call_id})
            transcript = None
        except Exception:  # noqa: BLE001 - an STT failure must not kill the call
            logger.exception("stt_call_failed")
            transcript = None
        logger.info("turn_timing_stt_final", extra={"seconds": round(loop.time() - stt_start, 3)})

        customer_text = transcript.text if transcript else ""
        if not customer_text.strip():
            await self._handle_empty_turn()
            return

        self._consecutive_empty_turns = 0
        await self._run_agent_turn(customer_text)

    async def _handle_empty_turn(self) -> None:
        """Silence, noise, or an STT failure/timeout produced nothing usable. A
        handful of these are normal (the customer pausing to think); past the
        configured threshold, proactively re-prompt with the script's own fallback
        line instead of leaving dead air on a live phone call."""
        self._consecutive_empty_turns += 1
        if self._consecutive_empty_turns < self._settings.max_consecutive_empty_turns:
            return
        self._consecutive_empty_turns = 0
        if self._speaking_task is not None and not self._speaking_task.done():
            return  # already talking; don't stack another utterance on top
        self._start_speaking(self._engine.current_fallback_response())

    def _start_speaking(self, text: str) -> None:
        self._first_frame_sent_at = None  # reset; _speak() sets this once real audio goes out
        self._speaking_task = asyncio.create_task(self._speak(text))

    async def _run_agent_turn(self, customer_text: str) -> None:
        turn_start = asyncio.get_event_loop().time()

        async def on_speech_ready(speech: str) -> None:
            # Fires as soon as the engine knows what to say — potentially before tool
            # execution for this turn has even started (see orchestrator/engine.py) —
            # so audio starts reaching the customer without waiting on a DB round trip.
            logger.info(
                "turn_timing_llm_proposal", extra={"seconds": round(asyncio.get_event_loop().time() - turn_start, 3)}
            )
            self._start_speaking(speech)

        async with self._session_factory() as session:
            turn_result = await self._engine.run_turn(
                customer_text,
                session=session,
                tenant_id=self._identity.tenant_id,
                campaign_id=self._identity.campaign_id,
                lead_id=self._identity.lead_id,
                conversation_id=self._identity.conversation_id,
                on_speech_ready=on_speech_ready,
            )

        if turn_result.end_call:
            if self._speaking_task is not None:
                await self._speaking_task
            try:
                await self._telephony.hangup_call(self._identity.provider_call_id)
            except Exception:  # noqa: BLE001 - a failed hangup must not crash the media loop
                # The session still ends below; routes that own their socket close it
                # once `ended` is set, which ends the stream on the provider side too.
                logger.exception("hangup_failed", extra={"call_id": self._identity.provider_call_id})
            await self.close()

    async def _speak(self, text: str) -> None:
        """Synthesizes and sends `text`, split into sentence-ish chunks whose TTS
        synthesis runs concurrently (`settings.enable_tts_sentence_pipelining`): every
        chunk's OpenAI request starts right away rather than waiting for the previous
        chunk to fully finish, so by the time chunk 1 has finished playing, chunk 2 is
        usually already partway (or fully) synthesized instead of starting cold."""
        chunks = split_into_speech_chunks(text) if self._settings.enable_tts_sentence_pipelining else [text]
        if not chunks:
            return

        queues: list[asyncio.Queue] = [asyncio.Queue(maxsize=16) for _ in chunks]

        async def _produce(chunk_text: str, queue: asyncio.Queue) -> None:
            try:
                async for pcm16_chunk in self._tts_provider.synthesize_stream(chunk_text):
                    await queue.put(pcm16_chunk)
            finally:
                await queue.put(None)  # sentinel: this chunk is done (or failed)

        producer_tasks = [asyncio.create_task(_produce(chunk_text, queue)) for chunk_text, queue in zip(chunks, queues)]

        bytes_per_sample = 1 if self._audio_encoding == "mulaw" else 2
        loop = asyncio.get_event_loop()
        next_send_time = loop.time()
        speak_start = next_send_time
        first_frame_logged = False

        async def _send_paced(frame: bytes) -> None:
            # Real-time pacing (Section 16/18): OpenAI's TTS stream doesn't arrive at a
            # steady rate — chunks can burst well ahead of real time. Without this, we
            # hand the telephony leg a burst of many frames almost instantly followed
            # by a gap, which is a classic cause of choppy/stuttery playback on a phone
            # line (the receiving side expects roughly one frame every frame_ms, not a
            # burst). We pace to a virtual clock advancing by each frame's own actual
            # duration (so the final, possibly-shorter flush()'d frame paces correctly
            # too) rather than sleeping a fixed amount per send. If processing ever
            # falls behind (e.g. a slow TTS chunk), we reset the baseline to "now"
            # instead of bursting to catch up — catching up would just recreate the
            # exact burst this exists to prevent.
            nonlocal next_send_time, first_frame_logged
            now = loop.time()
            send_time = now
            if next_send_time > now:
                await asyncio.sleep(next_send_time - now)
                send_time = next_send_time
            await self._telephony.send_audio(self._identity.provider_call_id, frame)
            if not first_frame_logged:
                first_frame_logged = True
                self._first_frame_sent_at = loop.time()
                logger.info("turn_timing_tts_first_frame", extra={"seconds": round(loop.time() - speak_start, 3)})
            duration_s = (len(frame) / bytes_per_sample) / TELEPHONY_SAMPLE_RATE_HZ
            next_send_time = send_time + duration_s

        try:
            frame_chunker = FrameChunker(
                frame_bytes=outbound_frame_bytes(self._settings.outbound_frame_ms, bytes_per_sample=bytes_per_sample)
            )
            for queue in queues:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if self._audio_encoding == "mulaw":
                        telephony_frame = tts_pcm16_to_telephony_frame(item, OPENAI_TTS_SAMPLE_RATE_HZ, self._resample_out)
                    else:
                        telephony_frame = tts_pcm16_to_telephony_pcm16(item, OPENAI_TTS_SAMPLE_RATE_HZ, self._resample_out)
                    for frame in frame_chunker.push(telephony_frame):
                        await _send_paced(frame)
            remainder = frame_chunker.flush()
            if remainder:
                await _send_paced(remainder)
        except asyncio.CancelledError:
            logger.info("tts_playback_cancelled", extra={"call_id": self._identity.provider_call_id})
            raise
        finally:
            for task in producer_tasks:
                task.cancel()
            for task in producer_tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def close(self) -> None:
        self._ended = True
        if self._speaking_task is not None and not self._speaking_task.done():
            self._speaking_task.cancel()
        if self._stt_stream is not None:
            await self._stt_stream.close()
        await self._tts_provider.close()
