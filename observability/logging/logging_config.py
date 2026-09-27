"""Structured logging setup (Section 24). Deliberately excludes raw PII (phone numbers,
transcript text) from ordinary log fields — anything that needs the full content
belongs in the DB (transcript_segment, qualification.evidence), accessed through
authorized queries, not grepped from logs.
"""
from __future__ import annotations

import logging

import structlog

from config.settings import get_settings

_REDACT_KEYS = {"phone", "phone_e164", "transcript", "speech", "text", "raw_phone"}


def _redact_pii(_, __, event_dict: dict) -> dict:
    for key in list(event_dict):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(message)s")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_pii,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(settings.log_level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)
