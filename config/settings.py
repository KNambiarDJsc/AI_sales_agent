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
    # Defaults chosen by measurement on this project's real prompt/schema (2026-10-03,
    # medians): LLM time-until-`speech`-is-ready gpt-4o 1.96s vs gpt-4.1-mini 0.84s
    # (gpt-5.4-nano 0.80s, gpt-4.1-nano 0.87s — all schema-valid 4/4); STT
    # gpt-4o-transcribe 0.94s vs gpt-4o-mini-transcribe 0.82s; TTS first audio
    # gpt-4o-mini-tts 0.80s vs tts-1 1.43s. Re-measure before changing.
    openai_stt_model: str = "gpt-4o-mini-transcribe"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"
    openai_llm_model: str = "gpt-4.1-mini"
    # Only for reasoning models (gpt-5.x): "none"/"minimal" keeps them fast. Leave
    # empty for gpt-4.x, which reject the parameter.
    openai_llm_reasoning_effort: str = ""
    # ISO-639-1 language the customer is transcribed in. Pinning it stops STT from
    # "detecting" another language on noise or accents (and the LLM then answering in
    # that language). Empty = auto-detect.
    stt_language: str = "en"

    # --- AI backend: OpenAI, or local models on this machine ---
    # "auto": OpenAI when its key works; each component (STT/LLM/TTS) falls back to
    #         the local model by itself the moment OpenAI refuses for an account
    #         reason (missing/invalid/expired key, no credits) — mid-call, same turn —
    #         and OpenAI is retried after `openai_retry_after_seconds`.
    # "openai": OpenAI only.   "local": local models only (no OpenAI calls at all).
    ai_backend: Literal["auto", "openai", "local"] = "auto"
    openai_retry_after_seconds: float = 300.0
    # Local LLM: Ollama's native API (structured output via a JSON-schema `format`,
    # plus control over context size and keeping the model resident in memory).
    local_llm_base_url: str = "http://127.0.0.1:11434"
    # Chosen by running whole scripted calls (interested / callback / not interested /
    # DNC) through the real engine on the dev laptop (Core Ultra 7 256V, Ollama on the
    # Arc GPU): llama3.2:3b passed 3/4, reply ready ~2.2 s median (3.2 s p90).
    # qwen2.5:1.5b and qwen3:1.7b passed 2/4 — and the 1.5B put an *interested* lead on
    # do-not-call; qwen3:4b 2/4 at ~3.8 s. qwen2.5:3b's licence is research-only (not for
    # client use). Bigger/faster hardware: re-run the comparison before changing this.
    local_llm_model: str = "llama3.2:3b"
    # Ollama's own default would silently drop the start of the prompt (the system
    # prompt) on long calls. 6144 tokens ≈ a 40-turn call; larger costs memory (the
    # KV cache: ~0.9 GB at 8192 for a 3B model) on machines that have little to spare.
    local_llm_num_ctx: int = 6144
    # Reasoning ("thinking") models such as qwen3 think before answering unless told
    # not to — seconds of silence on a phone call. False turns it off; None sends
    # nothing (for models without a thinking mode).
    local_llm_think: bool | None = False
    local_llm_temperature: float = 0.3
    local_llm_timeout_seconds: float = 20.0
    # Where local model files live (Kokoro ONNX weights + voices). Moonshine and Ollama
    # keep their own caches. `python scripts/setup_local_models.py` fills all three.
    local_models_dir: str = str(REPO_ROOT / "models")
    # Local TTS: Kokoro-82M, ONNX build (same model as `pip install kokoro`, but loads in
    # ~2 s instead of ~27 s for PyTorch, at the same speed). 24 kHz, like OpenAI TTS.
    local_tts_voice: str = "af_heart"
    local_tts_lang: str = "en-us"
    local_tts_speed: float = 1.0
    # CPU threads for Kokoro (0 = ONNX Runtime's default: every core). On a hybrid CPU
    # the slow efficiency cores hold every step back; measured on the dev laptop (Core
    # Ultra 7 256V: 4 performance + 4 low-power cores, on battery) a short phrase took
    # 3.5-5.3 s with all 8 threads and 2.3-3.0 s with 4. On a server with uniform cores,
    # set 0.
    local_tts_threads: int = 4
    # Local STT: Moonshine ONNX (useful-moonshine-onnx). Measured: base 0.23 s / 0.78 s /
    # 1.09 s for 1.5 / 6 / 8 s of speech; tiny ~2x faster but less accurate.
    local_stt_model: Literal["tiny", "base"] = "base"
    # CPU threads for Moonshine (0 = all cores) — same reason as local_tts_threads:
    # base measured 1.25 s median per utterance on all 8 cores vs 0.71 s on 4 (dev
    # laptop, on battery), identical transcripts. tiny on 4 threads was 0.46 s with the
    # same accuracy on clean test clips; not chosen because real phone audio is where
    # tiny loses accuracy and that wasn't measurable here.
    local_stt_threads: int = 4
    # Load local models at server start, so the first local reply isn't slowed by
    # model loading (Kokoro/Moonshine load, Ollama model into memory).
    local_preload: bool = True

    # STT backend: "buffered" (speech/stt/openai.py, REST, per-utterance — reliable,
    # zero surprises) or "realtime" (speech/stt/openai_realtime.py, genuine streaming
    # partials + fast finals via a websocket — confirmed working against a live key,
    # see STATUS.md, but has run one verification session, not production traffic).
    # Default stays "buffered" until "realtime" has been proven on an actual call.
    stt_backend: Literal["buffered", "realtime"] = "buffered"

    # --- Telephony ---
    telephony_provider: Literal["twilio", "exotel", "frejun", "vobiz"] = "twilio"

    # Public HTTPS base URL this app is reachable at (tunnel in dev, real host in
    # prod), e.g. "https://abc.trycloudflare.com". Every provider-facing URL is built
    # from it: FreJun's flow_url/ws_url/status_callback_url and the worker's media/
    # status-callback URLs. Falls back to TWILIO_WEBHOOK_BASE_URL, which used to carry
    # this for every provider despite its name.
    public_base_url: str = ""

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

    # FreJun / Teler (https://frejun.com/docs/teler/). The API key is the account's
    # single key (dashboard → Developers). The secret is the webhook signing secret
    # set on the Voice App that owns FREJUN_FROM_NUMBER — Teler signs every webhook
    # with it (HMAC-SHA256, X-Teler-Signature/X-Teler-Timestamp).
    frejun_api_key: str = ""
    frejun_api_base_url: str = "https://api.frejun.ai/api/v1"
    frejun_secret: str = ""
    frejun_from_number: str = ""  # E.164 Teler virtual number; must belong to a Voice App
    # Stream flow options (docs: telephony/call-flows). The `start` message currently
    # always advertises 8000 Hz regardless, so 8k is the honest setting. chunk_size is
    # how much caller audio each inbound message carries (20..2000, multiple of 20):
    # smaller means VAD/endpointing sees speech sooner, at the cost of more messages.
    frejun_sample_rate: Literal["8k", "16k"] = "8k"
    frejun_chunk_size_ms: int = Field(default=100, ge=20, le=2000, multiple_of=20)
    # Teler-side call recording. Off by default: no consent/disclosure policy has
    # been confirmed for this campaign (see STATUS.md placeholders).
    frejun_record: bool = False
    frejun_http_timeout_seconds: float = 10.0

    # Vobiz (https://vobiz.ai/docs). Auth ID + Auth Token from the console dashboard.
    # The token is also the key Vobiz signs callbacks with. VOBIZ_FROM_NUMBER must be a
    # number rented from (or the trial number assigned by) Vobiz — any other caller ID
    # fails with hangup cause 3030.
    vobiz_auth_id: str = ""
    vobiz_auth_token: str = ""
    vobiz_api_base_url: str = "https://api.vobiz.ai/api/v1"
    vobiz_from_number: str = ""
    vobiz_http_timeout_seconds: float = 10.0

    @property
    def effective_public_base_url(self) -> str:
        return (self.public_base_url or self.twilio_webhook_base_url).rstrip("/")

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
