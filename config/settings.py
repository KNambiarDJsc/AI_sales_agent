"""
Central application configuration.

Everything here is env-driven so the same codebase moves between local dev, staging,
and the client's production environment without code changes. Model names, provider
choice, and campaign defaults are all configurable per the architecture rule: never
hard-code provider model names or business logic in application code.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "config" / "prompts"
SCRIPTS_DIR = REPO_ROOT / "config" / "scripts"
QUALIFICATION_DIR = REPO_ROOT / "config" / "qualification"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: Literal["development", "staging", "production"] = "development"
    app_secret_key: str = "change-me"
    log_level: str = "INFO"

    # --- Database ---
    # Port 55434, not the default 5432 — see docker-compose.yml/.env.example for why
    # (avoids silently colliding with a locally-installed native Postgres on the host,
    # or with other local projects' docker Postgres containers).
    database_url: str = "postgresql+asyncpg://voiceagent:voiceagent@localhost:55434/voiceagent"
    database_url_sync: str = "postgresql+psycopg2://voiceagent:voiceagent@localhost:55434/voiceagent"

    # --- OpenAI ---
    openai_api_key: str = ""
    openai_stt_model: str = "gpt-4o-transcribe"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"
    openai_llm_model: str = "gpt-5.4-mini"

    # STT backend: "buffered" (speech/stt/openai.py, REST, per-utterance — reliable,
    # zero surprises) or "realtime" (speech/stt/openai_realtime.py, genuine streaming
    # partials + fast finals via a websocket — confirmed working against a live key,
    # see STATUS.md, but has run one verification session, not production traffic).
    # Default stays "buffered" until "realtime" has been proven on an actual call.
    stt_backend: Literal["buffered", "realtime"] = "buffered"

    # --- Telephony ---
    telephony_provider: Literal["twilio", "exotel", "freejun"] = "twilio"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_webhook_base_url: str = ""

    exotel_sid: str = ""
    exotel_api_key: str = ""
    exotel_api_token: str = ""
    exotel_subdomain: str = "api.exotel.com"
    exotel_caller_id: str = ""
    exotel_app_id: str = ""

    freejun_api_key: str = ""
    freejun_api_base_url: str = "https://api.frejun.ai/api/v1"
    freejun_caller_id: str = ""  # a Teler virtual number on the account, E.164

    # --- Campaign defaults (client-confirmable; never assume silently in business logic) ---
    default_call_window_start: str = "09:00"
    default_call_window_end: str = "19:00"
    default_max_call_duration_seconds: int = 420
    default_max_concurrent_calls: int = 3
    default_timezone: str = "Asia/Kolkata"

    # --- Observability ---
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "voice-sales-agent"

    # --- VAD / endpointing defaults (Section 17 of the architecture spec) ---
    # end_of_turn defaults to the tighter end of the client-approved 550-800ms band —
    # every ms here is dead air the customer hears before the agent responds.
    vad_speech_debounce_ms: int = Field(default=100, ge=80, le=150)
    vad_min_speech_segment_ms: int = Field(default=200, ge=150, le=250)
    vad_end_of_turn_ms: int = Field(default=600, ge=550, le=800)
    vad_max_turn_seconds: int = Field(default=25, ge=20, le=30)
    # Caught on a live Exotel call: barge-in fired before the agent's opening line had
    # sent a single frame, repeatedly, throughout the call — almost certainly the
    # agent's own TTS audio leaking back as "inbound" (acoustic/line echo on the real
    # phone leg; there's no acoustic echo on a browser-mic demo, which is why this
    # never surfaced before a real call). Each new utterance suppresses barge-in for
    # this long after it starts speaking, to absorb a brief echo blip right at onset; a
    # genuine human interruption is rarely over this fast, so real barge-ins still fire
    # normally once the grace window passes.
    barge_in_grace_ms: int = Field(default=500, ge=0, le=2000)

    # --- Realtime latency / resilience tuning ---
    # Hard deadlines so one slow provider call can't hang a live phone call
    # indefinitely (Section 23: "explicit deadlines"). Tune per observed p95s.
    llm_timeout_seconds: float = 8.0
    stt_timeout_seconds: float = 6.0
    tts_first_chunk_timeout_seconds: float = 5.0

    # Stream the LLM's structured-output response and start TTS on the `speech` field
    # as soon as it's fully decoded — before extracted_facts/tool_call/end_call finish
    # streaming — instead of waiting for the whole JSON object. Gated on the `state`
    # field passing the same allowed-transition check the non-streaming path uses
    # (orchestrator/streaming.py), so it never speaks text tied to a rejected
    # transition; full validation still always runs before any tool executes or the
    # state machine transitions. Verified against a live key (STATUS.md): streaming
    # does yield incremental deltas alongside strict `json_schema` (49 chunks for a
    # short response), and a full ConversationEngine.run_turn() round trip confirmed
    # the speech callback fires with text that exactly matches what full validation
    # later confirms. Keep watching logs for `speculative_tts_mismatch`/
    # `speculative_tts_error` in production regardless — that test was one account,
    # one session, not a load test.
    enable_speculative_tts: bool = True

    # Split multi-sentence agent responses into clauses and pipeline TTS synthesis
    # (start speaking sentence 1 while sentence 2 is still being synthesized) instead
    # of synthesizing the whole utterance as one request. Safe to leave on: a
    # single-sentence response behaves identically to the unsplit path.
    enable_tts_sentence_pipelining: bool = True

    # Backpressure guard (Section 16): if inbound audio piles up faster than VAD/STT
    # can drain it, drop the oldest audio rather than growing unbounded and adding
    # ever-increasing latency to every subsequent frame.
    max_pending_inbound_audio_bytes: int = 32_000  # ~1s of PCM16 16kHz mono

    # Outbound telephony audio is re-chunked to this many ms per media frame
    # regardless of the TTS provider's internal chunk size — matches Twilio/Exotel's
    # documented ~20ms media-frame convention and bounds how long a barge-in can take
    # to actually stop new audio going out (Section 18).
    outbound_frame_ms: int = 20

    # After this many consecutive turns where STT produced no usable transcript
    # (silence, noise, timeout), the agent proactively re-prompts instead of leaving
    # dead air on the line.
    max_consecutive_empty_turns: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
