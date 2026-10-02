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


# Every module in this codebase calls stdlib logging.getLogger(__name__).warning/info(
# msg, extra={...}) expecting those fields to show up — but logging.basicConfig's bare
# "%(message)s" format silently drops `extra` entirely (it only ever lived on the
# LogRecord object, never rendered). Caught while chasing a real latency bug: added
# extra={"seconds": ...} timing logs that printed nothing but the bare message. Rather
# than rewrite every call site to use structlog, this formatter renders whatever extra
# fields a given call actually passed, everywhere in the codebase at once.
_STANDARD_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "taskName"}


class _ExtraFieldsFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: ("[REDACTED]" if key.lower() in _REDACT_KEYS else value)
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS
        }
        return f"{base} {extras}" if extras else base


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler()
    handler.setFormatter(_ExtraFieldsFormatter("%(message)s"))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)

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
