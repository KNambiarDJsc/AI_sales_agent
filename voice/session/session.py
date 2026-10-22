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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from config.settings import get_settings
from orchestrator.engine import ConversationEngine
from speech.stt.base import STTProvider
from speech.tts.base import TTSProvider
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
)
from voice.session.sentence_split import split_into_speech_chunks
from voice.session import acknowledgments
from voice.session.speech_pipeline import phrases_from, synthesize_phrases
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
        self._stt_stream = None  # connected in the background by `start()`
        self._stt_connect: asyncio.Task | None = None
        self._stt_backlog = bytearray()  # audio heard before the STT stream was connected
        self._consecutive_empty_turns = 0
        # STT gets its own independent resample chain at whatever rate the provider
        # declares (e.g. OpenAIRealtimeSTTProvider requires >=24kHz, the VAD stays at
        # 16kHz regardless) — both derived straight from the same mu-law source, not
        # one resampled from the other.
        self._stt_input_rate_hz = stt_provider.input_sample_rate_hz
        self._resample_stt = ResampleState()

        self._speaking_task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        # Speech heard while the agent talks (see _process_vad_frame): how much, and
        # whether it has already interrupted the agent this utterance.
        self._overlap_speech_ms = 0
        self._barged_in_this_utterance = False
        self._first_frame_sent_at: float | None = None  # set in _speak(); see _process_vad_frame
        self._ended = False
        self._last_activity = 0.0  # loop time of the last speech from either side; see _watch_call
        self._started_at = 0.0  # loop time the call connected; see _watch_call
        self._wrapping_up = False  # time limit reached: saying goodbye, no more turns
        # Ended turns waiting for a reply, in order: (transcript task, talked_over).
        # Handled by _process_turns, never inline in handle_inbound_audio — awaiting a
        # whole LLM turn there stopped the call from listening (live Vobiz call: the
        # agent "got stuck" after the customer spoke, then audio arrived in a burst).
        self._turns: asyncio.Queue = asyncio.Queue()
        self._turn_worker: asyncio.Task | None = None

    async def start(self) -> None:
        # Not awaited: the greeting starts while the STT socket connects (~0.5 s that
        # used to be silence after pickup); audio heard meanwhile waits in _stt_backlog.
        self._stt_connect = asyncio.create_task(self._stt_provider.start_stream())
        self._background.add(warm := asyncio.create_task(self._engine.warm_up()))
        warm.add_done_callback(self._background.discard)
        self._last_activity = self._started_at = asyncio.get_event_loop().time()
        self._background.add(watch := asyncio.create_task(self._watch_call()))
        watch.add_done_callback(self._background.discard)
        self._prerecord_acknowledgments(delay_s=1.5)  # after the greeting has started

    def _prerecord_acknowledgments(self, delay_s: float = 0.0) -> None:
        phrases = self._engine.all_acknowledgments()
        if not phrases:
            return

        async def _later() -> None:
            await asyncio.sleep(delay_s)
            await acknowledgments.prerecord(self._tts_provider, phrases)

        self._background.add(task := asyncio.create_task(_later()))
        task.add_done_callback(self._background.discard)

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
        the opening line as to every other turn.

        A scripted opening plays immediately with no model call. It is started as a
        background speaking task, not awaited, so the caller's media loop keeps
        consuming inbound audio and barge-in still works during the greeting."""
        scripted = self._engine.scripted_opening()
        if scripted is not None:
            self._start_speaking(scripted)
            return
        await self._run_agent_turn("")

    async def handle_inbound_audio(self, audio_chunk: bytes) -> None:
        """Called by the WebSocket handler for every inbound media frame from the
        telephony provider, already in that provider's own encoding (mu-law 8kHz for
        Twilio, raw PCM16 8kHz for Exotel — see `TelephonyProvider.audio_encoding`)."""
        if self._ended or (self._stt_stream is None and self._stt_connect is None):
            return

        pcm16_native = mulaw_to_pcm16(audio_chunk) if self._audio_encoding == "mulaw" else audio_chunk

        # Feed the STT provider directly and continuously at its own required rate —
        # not gated on VAD-frame chunking below, since the STT stream just needs a
        # correctly-ordered byte stream, not 20ms-aligned frames.
        stt_frame = resample_pcm16(pcm16_native, TELEPHONY_SAMPLE_RATE_HZ, self._stt_input_rate_hz, self._resample_stt)
        if self._stt_stream is None and not self._stt_connect.done():
            self._stt_backlog.extend(stt_frame)
            del self._stt_backlog[: max(0, len(self._stt_backlog) - self._stt_input_rate_hz * 2 * 5)]  # last 5 s
        else:
            await (await self._stt()).send_audio(stt_frame)

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

    async def _watch_call(self) -> None:
        """Ends the call from our side when it must not go on:
        - the time limit (engine.call_limit_seconds(), 2 minutes): call_wrap_up_seconds
          before it the agent says the script's time_up_line and hangs up — the
          provider's own time_limit would cut it off mid-sentence;
        - settings.silence_hangup_seconds with nobody talking: the customer is gone
          (hung up without the provider noticing — seen with Vobiz — or walked away)."""
        loop = asyncio.get_event_loop()
        silence_limit = self._settings.silence_hangup_seconds
        wrap_up_at = self._engine.call_limit_seconds() - self._settings.call_wrap_up_seconds
        while not self._ended:
            await asyncio.sleep(1.0)
            now = loop.time()
            if now - self._started_at >= wrap_up_at:
                logger.info("call_time_limit", extra={"call_id": self._identity.provider_call_id})
                await self._end_from_our_side(goodbye=self._engine.time_up_line())
                return
            if self._agent_speaking():
                self._last_activity = now
                continue
            if silence_limit and now - self._last_activity >= silence_limit:
                logger.info("silence_hangup", extra={"call_id": self._identity.provider_call_id, "seconds": silence_limit})
                await self._end_from_our_side()
                return

    async def _end_from_our_side(self, goodbye: str = "") -> None:
        self._background.discard(asyncio.current_task())  # close() cancels background tasks — not this one
        self._wrapping_up = True
        if goodbye:
            # Let the current sentence finish briefly, then say goodbye (bounded: the
            # provider cuts the line at the limit anyway).
            if self._agent_speaking():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(self._speaking_task), 3.0)
                if self._agent_speaking():
                    await self._handle_barge_in()
            self._start_speaking(goodbye)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._speaking_task), self._settings.call_wrap_up_seconds - 1)
        try:
            await self._telephony.hangup_call(self._identity.provider_call_id)
        except Exception:  # noqa: BLE001 - the session still ends; the route closes the stream
            logger.exception("hangup_failed", extra={"call_id": self._identity.provider_call_id})
        await self.close()

    def _agent_speaking(self) -> bool:
        return self._speaking_task is not None and not self._speaking_task.done()

    async def _process_vad_frame(self, pcm16_frame: bytes) -> None:
        # STT already received this audio (at its own rate) from handle_inbound_audio;
        # this frame is 16kHz purely for VAD/endpointing.
        is_speech = self._vad.is_speech(pcm16_frame, frame_ms=VAD_FRAME_MS)
        if is_speech:
            self._last_activity = asyncio.get_event_loop().time()
        event = self._turn_taker.feed(is_speech, VAD_FRAME_MS)

        if event == TurnEvent.SPEECH_STARTED:
            self._overlap_speech_ms = 0
            self._barged_in_this_utterance = False
        # Barge-in (Section 18) needs the customer to actually talk over the agent for
        # settings.barge_in_min_speech_ms — not a "haan", a cough or line echo, which
        # each used to cut the agent off mid-sentence. Still never inside the grace
        # window right after our own audio starts (echo of it).
        if self._turn_taker.speech_in_progress and self._agent_speaking() and not self._barged_in_this_utterance:
            if is_speech:
                self._overlap_speech_ms += VAD_FRAME_MS
            if self._overlap_speech_ms >= self._settings.barge_in_min_speech_ms:
                grace_s = self._settings.barge_in_grace_ms / 1000.0
                started = self._first_frame_sent_at
                if started is None or asyncio.get_event_loop().time() - started >= grace_s:
                    self._barged_in_this_utterance = True
                    await self._handle_barge_in()

        if event in (TurnEvent.TURN_ENDED, TurnEvent.MAX_TURN_REACHED):
            # Short speech while the agent kept talking is usually a backchannel
            # ("haan", "okay") — but can be a real "no". Decided once it's transcribed.
            self._queue_turn(talked_over=self._agent_speaking() and not self._barged_in_this_utterance)

    def _queue_turn(self, talked_over: bool) -> None:
        # The transcript request starts *now*, so it covers exactly this utterance
        # (the realtime STT commits its buffer on the request), even if an earlier
        # turn is still being answered.
        transcript = asyncio.ensure_future(self._receive_final())
        self._turns.put_nowait((transcript, talked_over))
        if self._turn_worker is None or self._turn_worker.done():
            self._turn_worker = asyncio.create_task(self._process_turns())

    async def _receive_final(self):
        loop = asyncio.get_event_loop()
        stt_start = loop.time()
        try:
            stream = await self._stt()
            return await asyncio.wait_for(stream.receive_final(), timeout=self._settings.stt_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("stt_timeout", extra={"call_id": self._identity.provider_call_id})
            return None
        except Exception:  # noqa: BLE001 - an STT failure must not kill the call
            logger.exception("stt_call_failed")
            return None
        finally:
            logger.info("turn_timing_stt_final", extra={"seconds": round(loop.time() - stt_start, 3)})

    async def _stt(self):
        """The connected STT stream, with any audio heard before it connected sent
        first. Raises if connecting failed."""
        if self._stt_stream is None:
            stream = await self._stt_connect
            if self._stt_stream is None:
                self._stt_stream = stream
                backlog, self._stt_backlog = bytes(self._stt_backlog), bytearray()
                if backlog:
                    await stream.send_audio(backlog)
        return self._stt_stream

    async def _process_turns(self) -> None:
        while not self._ended:
            transcript_task, talked_over = await self._turns.get()
            try:
                transcript = await transcript_task
                text = transcript.text if transcript else ""
                if talked_over and (not text.strip() or acknowledgments.is_backchannel(text)):
                    # Don't reply to it — a reply queued behind the current one is
                    # what sounded like several agents.
                    logger.info("backchannel_ignored", extra={"call_id": self._identity.provider_call_id,
                                                              "text": text[:60]})
                    continue
                await self._on_turn_ended(text)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one failed turn must not end the conversation
                logger.exception("turn_failed", extra={"call_id": self._identity.provider_call_id})
            finally:
                self._turns.task_done()

    async def _turns_done(self) -> None:
        """Wait until every ended turn so far has been handled (tests)."""
        await self._turns.join()

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

    def _acknowledge(self) -> None:
        """Say a short pre-recorded "Okay." / "जी।" right now, while the transcript and
        the reply are being prepared (voice/session/acknowledgments.py). Skipped if the
        agent is still talking, or if the phrase isn't recorded yet (then it's recorded
        in the background for next time — never synthesized on this path)."""
        if self._speaking_task is not None and not self._speaking_task.done():
            return
        text = self._engine.acknowledgment()
        if text is None:
            return
        pcm = acknowledgments.cached(self._tts_provider, text)
        if pcm is None:
            self._prerecord_acknowledgments()
            return
        self._first_frame_sent_at = None
        self._speaking_task = asyncio.create_task(self._play(_once(pcm)))

    async def _on_turn_ended(self, customer_text: str) -> None:
        if not customer_text.strip():
            await self._handle_empty_turn()
            return

        self._consecutive_empty_turns = 0
        # Only now, with real words recognized: acknowledging at the end of every VAD
        # turn (first live call, 2026-10-22) also said "Okay." after line noise and
        # mid-sentence pauses — 8 stray acknowledgments in a 2-minute call, each one
        # then mistaken for the customer interrupting. ~0.7 s later than instant, still
        # ~2 s before the reply.
        self._acknowledge()
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
        recorded = acknowledgments.cached(self._tts_provider, text)  # e.g. the greeting (voice/session/opening.py)
        speech = self._play(_once(recorded)) if recorded is not None else self._speak(text)
        self._speaking_task = asyncio.create_task(self._after(self._speaking_task, speech))

    def _start_speaking_phrases(self, phrases: AsyncIterator[str]) -> None:
        self._first_frame_sent_at = None
        self._speaking_task = asyncio.create_task(self._after(self._speaking_task, self._speak_phrases(phrases)))

    @staticmethod
    async def _after(previous: asyncio.Task | None, speech) -> None:
        """Play `speech` once `previous` (what was playing when this was queued — an
        acknowledgment) has finished: never two audio streams interleaved on the line.
        A barge-in cancels this task, which also cancels `previous` while we wait on
        it — so the acknowledgment stops too."""
        try:
            if previous is not None and not previous.done():
                await previous
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                speech.close()
                raise  # we were cancelled (barge-in)
            # previous was cancelled on its own (e.g. a barge-in that already ended it)
        except Exception:  # noqa: BLE001 - a failed acknowledgment must not drop the reply
            logger.exception("acknowledgment_playback_failed")
        await speech

    async def _run_agent_turn(self, customer_text: str) -> None:
        if self._wrapping_up:
            return  # out of time: the goodbye is already playing
        turn_start = asyncio.get_event_loop().time()

        async def on_speech_stream(phrases: AsyncIterator[str]) -> None:
            # Fires with the reply's first phrase, while the LLM is still writing the
            # rest (see orchestrator/engine.py) — speaking starts now, and later
            # phrases flow into the same playback as they're written.
            logger.info(
                "turn_timing_first_phrase", extra={"seconds": round(asyncio.get_event_loop().time() - turn_start, 3)}
            )
            self._start_speaking_phrases(phrases)

        async with self._session_factory() as session:
            turn_result = await self._engine.run_turn(
                customer_text,
                session=session,
                tenant_id=self._identity.tenant_id,
                campaign_id=self._identity.campaign_id,
                lead_id=self._identity.lead_id,
                conversation_id=self._identity.conversation_id,
                on_speech_stream=on_speech_stream,
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
        chunk's request starts right away rather than waiting for the previous chunk to
        fully finish, so by the time chunk 1 has finished playing, chunk 2 is usually
        already partway (or fully) synthesized instead of starting cold."""
        chunks = split_into_speech_chunks(text) if self._settings.enable_tts_sentence_pipelining else [text]
        if chunks:
            await self._speak_phrases(phrases_from(chunks))

    async def _speak_phrases(self, phrases: AsyncIterator[str]) -> None:
        """Sends the audio for `phrases` (which may still be arriving — see
        `_run_agent_turn`), synthesizing each as soon as it exists
        (`voice/session/speech_pipeline.py`) and pacing frames in real time."""
        await self._play(synthesize_phrases(self._tts_provider, phrases))

    async def _play(self, audio: AsyncIterator[bytes]) -> None:
        """Sends TTS-rate PCM16 `audio` down the call, converted to the telephony
        encoding and paced in real time."""
        bytes_per_sample = 1 if self._audio_encoding == "mulaw" else 2
        out_rate = TELEPHONY_SAMPLE_RATE_HZ
        if self._audio_encoding == "pcm16":
            out_rate = getattr(self._telephony, "outbound_sample_rate_hz", TELEPHONY_SAMPLE_RATE_HZ)
        loop = asyncio.get_event_loop()
        next_send_time = loop.time()
        speak_start = next_send_time
        first_frame_logged = False
        lead_s = getattr(self._telephony, "outbound_lead_ms", 0) / 1000.0

        async def _send_paced(frame: bytes) -> None:
            # Real-time pacing (Section 16/18): see the history of this function — OpenAI's
            # TTS stream can burst ahead of real time, and a burst then a gap is choppy
            # on a phone line. Pace to a virtual clock advancing by each frame's actual
            # duration; if processing falls behind, reset to "now" rather than burst.
            nonlocal next_send_time, first_frame_logged
            now = loop.time()
            send_time = max(now, next_send_time)
            if next_send_time - lead_s > now:  # stay at most `lead_s` ahead of playback
                await asyncio.sleep(next_send_time - lead_s - now)
            await self._telephony.send_audio(self._identity.provider_call_id, frame)
            if not first_frame_logged:
                first_frame_logged = True
                self._first_frame_sent_at = loop.time()
                logger.info("turn_timing_tts_first_frame", extra={"seconds": round(loop.time() - speak_start, 3)})
            duration_s = (len(frame) / bytes_per_sample) / out_rate
            next_send_time = send_time + duration_s

        try:
            frame_ms = getattr(self._telephony, "outbound_frame_ms", None) or self._settings.outbound_frame_ms
            frame_chunker = FrameChunker(frame_bytes=outbound_frame_bytes(frame_ms, out_rate, bytes_per_sample))
            tts_rate = self._tts_provider.sample_rate_hz
            async for item in audio:
                if self._audio_encoding == "mulaw":
                    telephony_frame = tts_pcm16_to_telephony_frame(item, tts_rate, self._resample_out)
                else:
                    telephony_frame = resample_pcm16(item, tts_rate, out_rate, self._resample_out)
                for frame in frame_chunker.push(telephony_frame):
                    await _send_paced(frame)
            remainder = frame_chunker.flush()
            if remainder:
                await _send_paced(remainder)
        except asyncio.CancelledError:
            logger.info("tts_playback_cancelled", extra={"call_id": self._identity.provider_call_id})
            raise
        except Exception:  # noqa: BLE001 - a failed synthesis must not take the call down
            logger.exception("tts_playback_failed", extra={"call_id": self._identity.provider_call_id})
        finally:
            await audio.aclose()  # cancels any synthesis still in flight

    async def close(self) -> None:
        self._ended = True
        current = asyncio.current_task()  # close() may run inside the turn worker
        for task in [*self._background, self._turn_worker]:
            if task is not None and task is not current:
                task.cancel()
        if self._speaking_task is not None and not self._speaking_task.done():
            self._speaking_task.cancel()
        if self._stt_connect is not None and not self._stt_connect.done():
            self._stt_connect.cancel()
        elif self._stt_stream is None and self._stt_connect is not None and not self._stt_connect.cancelled():
            if self._stt_connect.exception() is None:
                self._stt_stream = self._stt_connect.result()
        if self._stt_stream is not None:
            await self._stt_stream.close()
        await self._tts_provider.close()


async def _once(pcm: bytes) -> AsyncIterator[bytes]:
    yield pcm
