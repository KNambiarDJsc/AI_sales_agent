"""Provider accounts linked from the console: stored encrypted, applied to the running
server without a restart.

The adapters (telephony/, speech/, llm/) read their credentials from
`config.settings`. Linking an account in the console saves its credentials
(services/accounts/vault.py) and sets the same settings attributes; unlinking blanks
them, so the server stops using that provider — even if .env still has a key. A
provider the console has never touched keeps whatever .env says ("from the server").

One row per provider in `agent_config` (key "provider:<id>"):
    {"linked": bool, "credentials": {field: sealed}, "balance": {...}, ...}
Accounts the client adds themselves ("other", e.g. a CRM or SMS service they want to
keep an eye on) are rows too, with a name and a recharge link but no credentials.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from database.models import AgentConfig
from services.accounts import vault
from services.accounts.registry import Provider, get_registry
from services.scripts.store import delete_value, get_value, set_value

logger = logging.getLogger(__name__)

PREFIX = "provider:"
SETTINGS_KEY = "account_settings"

_env_values: dict[str, str] | None = None  # what .env configured, before any console override


def _env() -> dict[str, str]:
    global _env_values
    if _env_values is None:
        s = get_settings()
        _env_values = {
            cred.setting: str(getattr(s, cred.setting, "") or "")
            for provider in get_registry().providers.values() for cred in provider.credentials
        }
    return _env_values


async def load_records(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, dict]:
    rows = (await session.execute(
        select(AgentConfig).where(AgentConfig.tenant_id == tenant_id, AgentConfig.key.like(f"{PREFIX}%"),
                                  AgentConfig.is_active.is_(True))
    )).scalars().all()
    return {row.key[len(PREFIX):]: dict(row.value) for row in rows}


def _resolved(provider: Provider, record: dict | None) -> tuple[dict[str, str], str, bool]:
    """(setting → value, source, unreadable). source: "console" (linked here),
    "server" (from .env), or "" (not linked)."""
    env = _env()
    if record is None:
        values = {c.setting: env.get(c.setting, "") for c in provider.credentials}
        linked = all(values[c.setting] for c in provider.credentials) if provider.credentials else False
        return values, ("server" if linked else ""), False
    if not record.get("linked"):
        return {c.setting: "" for c in provider.credentials}, "", False
    sealed = record.get("credentials") or {}
    values, unreadable = {}, False
    for cred in provider.credentials:
        value = vault.unseal(sealed[cred.field]) if cred.field in sealed else None
        if value is None:
            unreadable = unreadable or cred.field in sealed
            value = env.get(cred.setting, "")  # stored one unusable → the server's own
        values[cred.setting] = value
    # Linked with nothing stored = "use the server's own keys" (.env).
    return values, ("console" if sealed else "server"), unreadable


def apply_records(records: dict[str, dict]) -> list[str]:
    """Set the credentials the adapters read. Returns the providers whose values changed."""
    settings = get_settings()
    changed: list[str] = []
    for provider in get_registry().providers.values():
        values, _, _ = _resolved(provider, records.get(provider.id))
        for setting, value in values.items():
            if str(getattr(settings, setting, "") or "") != value:
                setattr(settings, setting, value)
                if provider.id not in changed:
                    changed.append(provider.id)
    if changed:
        _reset_clients(changed)
        logger.info("provider_accounts_applied", extra={"changed": changed})
    return changed


def _reset_clients(changed: list[str]) -> None:
    """Drop cached clients built with the old credentials."""
    if "openai" in changed:
        from llm import openai_client

        openai_client._clients.clear()
        openai_client._no_loop_client = None
        openai_client.reset_openai_health()
    if "vobiz" in changed:
        from telephony.factory import get_telephony_provider

        # A call in progress keeps the provider object it started with.
        get_telephony_provider.cache_clear()


async def refresh(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    apply_records(await load_records(session, tenant_id))


def describe(provider: Provider, record: dict | None) -> dict:
    """The provider's link state for the console — secrets masked."""
    values, source, unreadable = _resolved(provider, record)
    fields = []
    for cred in provider.credentials:
        value = values.get(cred.setting, "")
        fields.append({"field": cred.field, "label": cred.label, "secret": cred.secret,
                       "value": vault.mask(value) if cred.secret else value, "set": bool(value)})
    linked = bool(source) and all(f["set"] for f in fields)
    return {"linked": linked, "source": source if linked else "", "needs_relink": unreadable, "fields": fields,
            "can_use_server_keys": all(_env().get(c.setting) for c in provider.credentials) and bool(provider.credentials)}


async def verify(provider: Provider, values: dict[str, str]) -> tuple[bool, str]:
    """Check new credentials against the provider before saving them. Uses only calls
    the adapters already make (each traced to the provider's docs)."""
    try:
        if provider.id == "openai":
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=values["api_key"], max_retries=0)
            try:
                await asyncio.wait_for(client.models.list(), timeout=10)
            finally:
                await client.close()
            return True, "OpenAI accepted the key."
        if provider.id == "vobiz":
            from telephony.vobiz import VobizProvider

            vobiz = VobizProvider.with_credentials(values["auth_id"], values["auth_token"])
            balance = await asyncio.wait_for(vobiz.get_balance("INR"), timeout=10)
            return True, f"Vobiz accepted the credentials (balance ₹{float(balance.get('available_balance') or 0):.2f})."
        if provider.id == "sarvam":
            from speech.tts.sarvam import SarvamTTSProvider

            tts = SarvamTTSProvider(api_key=values["api_key"])

            async def _first_audio() -> None:
                async for _ in tts.synthesize_stream("जी"):  # one syllable: a fraction of a paisa
                    break

            await asyncio.wait_for(_first_audio(), timeout=10)
            return True, "Sarvam accepted the key."
    except asyncio.TimeoutError:
        return False, f"{provider.name} didn't answer in time. Check the internet connection and try again."
    except Exception as exc:  # noqa: BLE001 - shown to the client, with any key removed
        text = str(exc)
        for value in values.values():
            if value:
                text = text.replace(value, "<hidden>")
        return False, f"{provider.name} refused these credentials: {text[:180]}"
    return True, ""


async def link(session: AsyncSession, tenant_id: uuid.UUID, provider: Provider, values: dict[str, str]) -> None:
    record = await get_value(session, tenant_id, PREFIX + provider.id) or {}
    record.update({
        "linked": True,
        "credentials": {c.field: vault.seal(values[c.field].strip()) for c in provider.credentials},
        "linked_at": datetime.now(timezone.utc).isoformat(),
    })
    await set_value(session, tenant_id, PREFIX + provider.id, record)
    await refresh(session, tenant_id)


async def link_server_keys(session: AsyncSession, tenant_id: uuid.UUID, provider: Provider) -> None:
    """Back to the keys in the server's .env (forget what the console stored)."""
    record = await get_value(session, tenant_id, PREFIX + provider.id) or {}
    keep = {k: v for k, v in record.items() if k not in ("linked", "credentials", "linked_at")}
    if keep:
        # No "linked" key at all = "the console hasn't decided": .env applies.
        await set_value(session, tenant_id, PREFIX + provider.id, {**keep, "linked": True, "credentials": {}})
    else:
        await delete_value(session, tenant_id, PREFIX + provider.id)
    await refresh(session, tenant_id)


async def unlink(session: AsyncSession, tenant_id: uuid.UUID, provider: Provider) -> None:
    record = await get_value(session, tenant_id, PREFIX + provider.id) or {}
    record.pop("credentials", None)
    record["linked"] = False
    await set_value(session, tenant_id, PREFIX + provider.id, record)
    await refresh(session, tenant_id)


async def update_record(session: AsyncSession, tenant_id: uuid.UUID, account_id: str, changes: dict) -> dict:
    record = await get_value(session, tenant_id, PREFIX + account_id) or {}
    if account_id in get_registry().providers and "linked" not in record:
        # Saving a balance must not change which keys are in use.
        record = {**record, "linked": True, "credentials": {}}
    record.update(changes)
    await set_value(session, tenant_id, PREFIX + account_id, record)
    return record


def blocking_problems() -> list[str]:
    """Why a call can't be placed right now, in the client's words (empty = ready)."""
    s = get_settings()
    problems = []
    if s.telephony_provider == "vobiz" and not (s.vobiz_auth_id and s.vobiz_auth_token and s.vobiz_from_number):
        problems.append("The phone line (Vobiz) isn't linked. Link it on the Accounts page.")
    if s.ai_backend != "local" and not s.openai_api_key:
        problems.append("OpenAI isn't linked, so the agent can't think. Link it on the Accounts page.")
    return problems
