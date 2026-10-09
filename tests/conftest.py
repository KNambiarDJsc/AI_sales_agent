"""Test-wide settings. Tests drive finalize_call, which schedules a rebuild of the
Excel call log (workers/call_log.py); that must never write the real
exports/call_log.xlsx from a test run."""
import pytest

from config.settings import get_settings


@pytest.fixture(autouse=True)
def _no_call_log_writes(monkeypatch):
    monkeypatch.setattr(get_settings(), "call_log_enabled", False)
    monkeypatch.setattr("services.call_worker.lifecycle.schedule_call_log_refresh", lambda *a, **k: None)
