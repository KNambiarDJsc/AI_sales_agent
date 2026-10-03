"""Place ONE controlled Exotel test call to a single number.

    python scripts/exotel_test_call.py +9198XXXXXXXX [--name "Naman"] [--business "Test Business"]
    python scripts/exotel_test_call.py --check          # preflight only, dials nothing

Goes through the exact function the production worker uses
(`services/call_worker/dispatch.py:dispatch_next_call`) rather than a special path, but
against a dedicated test campaign holding exactly one lead, so nothing else can be
dialled. The campaign is paused again afterwards, so a running `workers.call_worker`
never re-dials it. Never prints credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import phonenumbers
from sqlalchemy import select

from config.settings import get_settings
from database.models import CallAttempt, Campaign, Lead, Tenant
from database.session import engine, session_scope
from services.call_worker.dispatch import dispatch_next_call
from telephony.factory import get_telephony_provider

TEST_TENANT = "Exotel Test Tenant"
TEST_CAMPAIGN = "Exotel Test Campaign"


def preflight() -> list[str]:
    s = get_settings()
    problems = []
    for field in ("exotel_sid", "exotel_api_key", "exotel_api_token", "exotel_caller_id", "exotel_app_id"):
        if not getattr(s, field):
            problems.append(f"{field.upper()} is empty in .env")
    base = s.twilio_webhook_base_url
    if not base.startswith("https://") or "your-public-host" in base:
        problems.append("TWILIO_WEBHOOK_BASE_URL must be the public https tunnel URL (it is used for every provider)")
    else:
        try:
            if httpx.get(f"{base}/health", timeout=15).status_code != 200:
                problems.append(f"{base}/health did not return 200 — is the tunnel/server up?")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{base}/health unreachable ({type(exc).__name__}) — is the tunnel up?")
    if not s.openai_api_key:
        problems.append("OPENAI_API_KEY is empty in .env")
    return problems


async def place_call(phone_e164: str, name: str, business: str) -> int:
    settings = get_settings()
    base = settings.twilio_webhook_base_url
    async with session_scope() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.name == TEST_TENANT))).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name=TEST_TENANT)
            session.add(tenant)
            await session.flush()

        campaign = (
            await session.execute(select(Campaign).where(Campaign.tenant_id == tenant.id, Campaign.name == TEST_CAMPAIGN))
        ).scalar_one_or_none()
        if campaign is None:
            campaign = Campaign(
                tenant_id=tenant.id, name=TEST_CAMPAIGN, script_id="product-a", script_version=1, status="paused",
                telephony_provider="exotel", call_window_start="00:00", call_window_end="23:59",
                timezone=settings.default_timezone, max_concurrent_calls=1, max_call_duration_seconds=420,
            )
            session.add(campaign)
            await session.flush()

        # Exactly one dialable lead in this campaign: the number given on the command line.
        for other in (await session.execute(select(Lead).where(Lead.campaign_id == campaign.id))).scalars():
            if other.status in ("pending", "queued"):
                other.status = "completed"
        # A previous test call that never got a final status would count against the
        # campaign's concurrency limit of 1 and silently block this one.
        for stale in (
            await session.execute(
                select(CallAttempt).where(
                    CallAttempt.campaign_id == campaign.id,
                    CallAttempt.status.in_(["queued", "dialing", "ringing", "in_progress"]),
                )
            )
        ).scalars():
            stale.status = "canceled"

        lead = (
            await session.execute(select(Lead).where(Lead.campaign_id == campaign.id, Lead.dedupe_key == phone_e164))
        ).scalar_one_or_none()
        if lead is None:
            lead = Lead(tenant_id=tenant.id, campaign_id=campaign.id, phone_e164=phone_e164, raw_phone=phone_e164,
                        dedupe_key=phone_e164, contact_name=name, business_name=business, status="pending")
            session.add(lead)
        else:
            lead.contact_name, lead.business_name, lead.status = name, business, "pending"
        campaign.status = "active"
        await session.flush()
        lead_id, campaign_id = lead.id, campaign.id

        try:
            dispatched = await dispatch_next_call(
                session, campaign, get_telephony_provider("exotel"),
                media_websocket_base_url=f"{base.replace('https://', 'wss://')}/media",
                status_callback_base_url=f"{base}/webhooks",
            )
        finally:
            campaign.status = "paused"

    async with session_scope() as session:
        attempt = (
            await session.execute(
                select(CallAttempt).where(CallAttempt.campaign_id == campaign_id, CallAttempt.lead_id == lead_id)
                .order_by(CallAttempt.attempt_number.desc()).limit(1)
            )
        ).scalar_one_or_none()
        lead = await session.get(Lead, lead_id)
        print(f"dispatched: {dispatched}")
        if attempt is not None:
            print(f"call attempt: id={attempt.id} number={attempt.attempt_number} status={attempt.status}")
            print(f"exotel call sid: {attempt.provider_call_id}")
            if attempt.error:
                print(f"error: {attempt.error}")
        print(f"lead status: {lead.status}")
    await engine.dispose()
    return 0 if dispatched else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("phone", nargs="?", help="number to call, e.g. +9198XXXXXXXX")
    parser.add_argument("--name", default="there")
    parser.add_argument("--business", default="your business")
    parser.add_argument("--check", action="store_true", help="run the preflight only; dial nothing")
    args = parser.parse_args()

    problems = preflight()
    for problem in problems:
        print(f"NOT READY: {problem}")
    if args.check or problems:
        if not problems:
            print("preflight OK")
        return 1 if problems else 0
    if not args.phone:
        parser.error("phone number required")

    try:
        parsed = phonenumbers.parse(args.phone, "IN")
    except phonenumbers.NumberParseException:
        parser.error(f"could not parse phone number: {args.phone!r}")
    if not phonenumbers.is_valid_number(parsed):
        parser.error(f"not a valid phone number: {args.phone!r}")
    phone_e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return asyncio.run(place_call(phone_e164, args.name, args.business))


if __name__ == "__main__":
    raise SystemExit(main())
