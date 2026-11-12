"""Costs and balances for the console, from the calls actually made.

- `costs_for(rows)`: each call's estimated cost (the phone line's share is the
  provider's real charge when it reports one).
- `account_balances(...)`: per account, either the live balance from the provider's
  API or — where the provider offers none — the amount the client said they had,
  minus what calls have used since ("estimated").
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from config.settings import get_settings
from services.accounts.costs import Stack, call_cost, current_stack, typical_call_cost
from services.accounts.registry import get_registry
from workers.call_log import CallRow

logger = logging.getLogger(__name__)

_ACTUALS_TTL_SECONDS = 60
_actuals: tuple[float, dict[str, float]] = (0.0, {})
_actuals_lock = asyncio.Lock()
_balance_cache: dict[str, tuple[float, float | None]] = {}
_BALANCE_TTL_SECONDS = 30


async def telephony_actuals() -> dict[str, float]:
    """provider_call_id → what the phone provider charged for that call (rupees).
    Vobiz only (its CDR list); empty when unavailable — estimates are used then."""
    global _actuals
    s = get_settings()
    if s.telephony_provider != "vobiz" or not (s.vobiz_auth_id and s.vobiz_auth_token):
        return {}
    if time.monotonic() - _actuals[0] < _ACTUALS_TTL_SECONDS:
        return _actuals[1]
    async with _actuals_lock:
        if time.monotonic() - _actuals[0] < _ACTUALS_TTL_SECONDS:
            return _actuals[1]
        from telephony.factory import get_telephony_provider

        found: dict[str, float] = dict(_actuals[1])  # keep what we know if a page fails
        try:
            provider = get_telephony_provider("vobiz")
            for page in range(1, 51):  # newest first; 5,000 calls is plenty between restarts
                body = await asyncio.wait_for(provider.list_cdrs(page=page, per_page=100), 10)
                for record in body.get("data") or []:
                    if record.get("uuid") and record.get("billing_status") == "completed":
                        found[str(record["uuid"])] = float(record.get("total_cost") or 0)
                if not (body.get("pagination") or {}).get("has_next"):
                    break
        except Exception as exc:  # noqa: BLE001 - estimates still work
            logger.info("telephony_actuals_unavailable", extra={"error": f"{type(exc).__name__}: {exc}"[:160]})
        _actuals = (time.monotonic(), found)
        return found


def row_cost(row: CallRow, actuals: dict[str, float], stack: Stack, usd_inr: float) -> dict:
    return call_cost(
        connected=row.connected, line_seconds=row.line_seconds, agent_chars=row.agent_chars, llm_turns=row.llm_turns,
        prompt_tokens=row.prompt_tokens, cached_tokens=row.cached_tokens, completion_tokens=row.completion_tokens,
        llm_model=row.llm_model, telephony_actual=actuals.get(row.provider_call_id) if row.provider_call_id else None,
        stack=stack, usd_inr=usd_inr,
    )


async def costs_for(rows: list[CallRow], usd_inr: float | None = None) -> dict[str, dict]:
    """attempt id → cost breakdown, for these rows."""
    actuals = await telephony_actuals()
    stack = current_stack()
    rate = usd_inr or get_registry().usd_inr
    return {row.attempt_id: row_cost(row, actuals, stack, rate) for row in rows}


async def live_balance(provider_id: str) -> float | None:
    """The provider's own balance figure, where it has an API for it."""
    cached = _balance_cache.get(provider_id)
    if cached and time.monotonic() - cached[0] < _BALANCE_TTL_SECONDS:
        return cached[1]
    value: float | None = None
    if provider_id == "vobiz":
        s = get_settings()
        if s.vobiz_auth_id and s.vobiz_auth_token:
            try:
                from telephony.factory import get_telephony_provider

                body = await asyncio.wait_for(get_telephony_provider("vobiz").get_balance("INR"), 8)
                value = float(body.get("available_balance"))
            except Exception:  # noqa: BLE001
                value = None
    _balance_cache[provider_id] = (time.monotonic(), value)
    return value


def forget_balances() -> None:
    _balance_cache.clear()


def spent_since(rows: list[CallRow], costs: dict[str, dict], provider_id: str, since: datetime | None) -> float:
    """Rupees this provider's share of the calls came to since `since`."""
    total = 0.0
    for row in rows:
        if since is not None and (row.started_utc is None or row.started_utc < since):
            continue
        total += (costs.get(row.attempt_id) or {}).get("by_provider", {}).get(provider_id, 0.0)
    return total


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def per_call_share(provider_id: str, seconds: int, usd_inr: float) -> float:
    return typical_call_cost(seconds, usd_inr=usd_inr)["by_provider"].get(provider_id, 0.0)
