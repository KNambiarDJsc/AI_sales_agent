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
    database_url: str = "postgresql+asyncpg://voiceagent:voiceagent@localhost:5432/voiceagent"
    database_url_sync: str = "postgresql+psycopg2://voiceagent:voiceagent@localhost:5432/voiceagent"

    # --- OpenAI ---
    openai_api_key: str = ""
    openai_stt_model: str = "gpt-4o-transcribe"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"
    openai_llm_model: str = "gpt-4o"

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
    freejun_api_base_url: str = ""

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
    vad_speech_debounce_ms: int = Field(default=100, ge=80, le=150)
    vad_min_speech_segment_ms: int = Field(default=200, ge=150, le=250)
    vad_end_of_turn_ms: int = Field(default=700, ge=550, le=800)
    vad_max_turn_seconds: int = Field(default=25, ge=20, le=30)


@lru_cache
def get_settings() -> Settings:
    return Settings()
