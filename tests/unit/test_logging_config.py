import logging

from observability.logging.logging_config import configure_logging


def test_extra_fields_are_rendered_not_silently_dropped():
    # Regression test for a real bug caught while chasing a reported latency issue:
    # every module in this codebase logs via stdlib logging.getLogger(__name__) with
    # extra={...}, but logging.basicConfig's bare "%(message)s" format silently drops
    # `extra` entirely - added turn_timing_*(extra={"seconds": ...}) logs that printed
    # nothing but the bare message, with no indication anything was wrong.
    configure_logging()
    root_handler = logging.getLogger().handlers[0]
    record = logging.getLogger("test_logging_config").makeRecord(
        "test_logging_config", logging.INFO, __file__, 0, "some_event", (), None,
        extra={"seconds": 1.234, "call_id": "abc123"},
    )
    rendered = root_handler.format(record)
    assert "1.234" in rendered
    assert "abc123" in rendered


def test_phone_numbers_still_get_redacted_in_extra_fields():
    configure_logging()
    root_handler = logging.getLogger().handlers[0]
    record = logging.getLogger("test_logging_config").makeRecord(
        "test_logging_config", logging.INFO, __file__, 0, "lead_dialed", (), None,
        extra={"phone": "+917045210259"},
    )
    rendered = root_handler.format(record)
    assert "+917045210259" not in rendered
    assert "REDACTED" in rendered


def test_message_with_no_extra_fields_is_unaffected():
    configure_logging()
    root_handler = logging.getLogger().handlers[0]
    record = logging.getLogger("test_logging_config").makeRecord(
        "test_logging_config", logging.INFO, __file__, 0, "plain_message", (), None,
    )
    assert root_handler.format(record) == "plain_message"
