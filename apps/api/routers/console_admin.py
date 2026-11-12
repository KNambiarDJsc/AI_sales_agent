"""Console API, part 2: call scripts and provider accounts.

- **Scripts** — the client's own call scripts: upload a .txt / Word / PDF file (or paste
  text), review what the agent will say, choose which script calls use
  (services/scripts/). Built-in scripts are config/call_scripts/*.yaml.
- **Accounts** — the services calls run on (config/providers.yaml): link / unlink
  credentials, balances, recharge links, and what a call costs (services/accounts/).

Everything needs the console login. Credentials are never returned — only masked.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from apps.api.console_auth import require_console
from apps.api.routers.console import _campaigns, _console_rows
from config.settings import get_settings
from database.models import Campaign, Tenant
from database.session import session_scope
from orchestrator.call_script import (
    LEAD_FIELDS,
    MAX_SCRIPT_CHARS,
    OPENING_FIELDS,
    CallScript,
    apply_to_flow,
    builtin_call_scripts,
    script_fields,
)
from orchestrator.state_machine import StateMachine, load_script_by_id
from services.accounts import runtime, usage
from services.accounts.costs import current_stack, typical_call_cost
from services.accounts.registry import get_registry
from services.scripts import store
from services.scripts.compiler import compile_script
from services.scripts.documents import SUPPORTED, ScriptFileError, extract_text, tidy
from workers.call_log import collect_rows

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/console", tags=["console"], dependencies=[Depends(require_console)])


async def _tenant_id(session):
    leads_c, _ = await _campaigns(session)
    return leads_c.tenant_id


# --------------------------------------------------------------------------- scripts


class ScriptBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    language_style: str = "hinglish"
    agent_name: str = Field(default="", max_length=40)
    company_name: str = Field(default="", max_length=80)
    opening_lines: list[str] = Field(default_factory=list, max_length=6)
    script_text: str = Field(min_length=20)
    knowledge: dict[str, str] = Field(default_factory=dict)
    objection_playbook: dict[str, str] = Field(default_factory=dict)
    product_info: str = Field(default="", max_length=600)
    target_customer: str = Field(default="", max_length=400)
    source: str = "written"
    source_filename: str = Field(default="", max_length=200)


def _friendly(exc: ValidationError) -> str:
    first = exc.errors()[0]
    return str(first.get("msg", "Invalid script")).removeprefix("Value error, ")


def _build(script_id: str, body: ScriptBody) -> CallScript:
    try:
        return CallScript(id=script_id, **{
            **body.model_dump(),
            "source": body.source if body.source in ("uploaded", "written") else "written",
            "knowledge": {k.strip(): v.strip() for k, v in body.knowledge.items() if k.strip() and v.strip()},
            "objection_playbook": {k.strip(): v.strip() for k, v in body.objection_playbook.items()
                                   if k.strip() and v.strip()},
        })
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_friendly(exc)) from exc


def _summary(script: CallScript, active_id: str, edited: bool) -> dict:
    return {
        "id": script.id, "name": script.name, "description": script.description,
        "language_style": script.language_style, "agent_name": script.agent_name,
        "company_name": script.company_name, "source": script.source, "source_filename": script.source_filename,
        "active": script.id == active_id, "builtin": script.id in builtin_call_scripts(),
        "edited": edited, "chars": len(script.script_text),
        "opening_line": script.opening_lines[0] if script.opening_lines else "",
    }


@router.get("/scripts")
async def list_scripts() -> dict:
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        scripts = await store.list_scripts(session, tenant_id)
        active_id = await store.active_script_id(session, tenant_id)
        builtin = builtin_call_scripts()
        return {
            "active_id": active_id,
            "scripts": [_summary(s, active_id, edited=s.id in builtin and s != builtin[s.id]) for s in scripts],
            "fields": list(OPENING_FIELDS), "lead_fields": list(LEAD_FIELDS),
            "max_chars": MAX_SCRIPT_CHARS, "file_types": list(SUPPORTED),
        }


@router.get("/scripts/{script_id}")
async def get_script(script_id: str) -> dict:
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        script = await store.get_script(session, tenant_id, script_id)
        if script is None:
            raise HTTPException(status_code=404, detail="Script not found")
        active_id = await store.active_script_id(session, tenant_id)
        builtin = builtin_call_scripts()
        return {**script.model_dump(), "active": script.id == active_id, "builtin": script.id in builtin,
                "edited": script.id in builtin and script != builtin[script.id]}


async def _draft_from(text: str, filename: str) -> dict:
    if len(text) > 60_000:
        raise HTTPException(status_code=413, detail="That is too long for a call script (over 60,000 characters). "
                                                    "Upload only the part the agent should say.")
    draft, organised = await compile_script(text, filename)
    return {"draft": {**draft, "source": "uploaded" if filename else "written", "source_filename": filename},
            "organised": organised, "original_text": text[:60_000], "original_chars": len(text)}


@router.post("/scripts/read")
async def read_script_file(file: UploadFile) -> dict:
    """Read an uploaded script and sort it into a draft. Nothing is saved yet: the
    client reviews (and can edit) the draft, then saves it."""
    data = await file.read()
    try:
        text = extract_text(file.filename or "", data)
    except ScriptFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _draft_from(text, (file.filename or "script")[:200])


class PastedScript(BaseModel):
    text: str = Field(min_length=20)


@router.post("/scripts/read-text")
async def read_script_text(body: PastedScript) -> dict:
    return await _draft_from(tidy(body.text), "")


@router.post("/scripts")
async def create_script(body: ScriptBody) -> dict:
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        script = _build(await store.new_script_id(session, tenant_id, body.name), body)
        await store.save_script(session, tenant_id, script)
        return {"id": script.id}


@router.put("/scripts/{script_id}")
async def update_script(script_id: str, body: ScriptBody) -> dict:
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        existing = await store.get_script(session, tenant_id, script_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Script not found")
        script = _build(script_id, body)
        if script_id in builtin_call_scripts():
            script = script.model_copy(update={"source": "builtin"})
        else:
            script = script.model_copy(update={"source": existing.source, "source_filename": existing.source_filename})
        await store.save_script(session, tenant_id, script)
        return {"id": script.id}


@router.delete("/scripts/{script_id}")
async def delete_script(script_id: str) -> dict:
    """Delete the client's script; for a built-in one, undo their edits."""
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        removed = await store.delete_script(session, tenant_id, script_id)
        if not removed:
            if script_id in builtin_call_scripts():
                raise HTTPException(status_code=409, detail="This is a built-in script. It can be edited, not deleted.")
            raise HTTPException(status_code=404, detail="Script not found")
        return {"deleted": True, "reset_to_original": script_id in builtin_call_scripts()}


@router.post("/scripts/{script_id}/activate")
async def activate_script(script_id: str) -> dict:
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        if await store.get_script(session, tenant_id, script_id) is None:
            raise HTTPException(status_code=404, detail="Script not found")
        await store.set_active_script(session, tenant_id, script_id)
        return {"active_id": script_id}


class OpeningPreview(BaseModel):
    opening_lines: list[str] = Field(default_factory=list)
    agent_name: str = ""
    company_name: str = ""
    language_style: str = "hinglish"
    lead: dict[str, str] = Field(default_factory=dict)


@router.post("/scripts/preview-opening")
async def preview_opening(body: OpeningPreview) -> dict:
    """The exact first line a lead would hear — for the script editor."""
    try:
        draft = CallScript(id="preview", name="preview", opening_lines=body.opening_lines, agent_name=body.agent_name,
                           company_name=body.company_name, language_style=body.language_style)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_friendly(exc)) from exc
    flow = apply_to_flow(load_script_by_id(get_settings().console_script_id), draft)
    fields = {**{k: v for k, v in body.lead.items() if v.strip()}, **script_fields(draft)}
    line = StateMachine(flow).opening_line(fields)
    return {"line": line or "", "from_script": bool(body.opening_lines) and bool(line)}


# --------------------------------------------------------------------------- accounts


async def _usd_inr(session, tenant_id) -> float:
    saved = await store.get_value(session, tenant_id, runtime.SETTINGS_KEY) or {}
    try:
        value = float(saved.get("usd_inr") or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if 50 <= value <= 200 else get_registry().usd_inr


async def _all_rows(session, tenant_ids_campaigns: set) -> list:
    """Console calls plus browser-demo sessions: both use the AI accounts."""
    demo = (await session.execute(
        select(Campaign.id).join(Tenant, Tenant.id == Campaign.tenant_id).where(Tenant.name == "Browser Demo Tenant")
    )).scalars().all()
    return await collect_rows(campaign_ids=tenant_ids_campaigns | set(demo), apply_skip_filter=False)


def _money(amount: float | None, currency: str, usd_inr: float) -> dict | None:
    if amount is None:
        return None
    return {"amount": round(amount, 2), "currency": currency,
            "inr": round(amount * usd_inr, 2) if currency == "USD" else round(amount, 2)}


async def accounts_overview() -> dict:
    registry = get_registry()
    s = get_settings()
    async with session_scope() as session:
        leads_c, test_c = await _campaigns(session)
        tenant_id = leads_c.tenant_id
        records = await runtime.load_records(session, tenant_id)
        usd_inr = await _usd_inr(session, tenant_id)
        rows = await _all_rows(session, {leads_c.id, test_c.id})
    costs = await usage.costs_for(rows, usd_inr)
    limit = s.default_max_call_duration_seconds
    typical = typical_call_cost(limit, usd_inr=usd_inr)
    one_minute = typical_call_cost(60, usd_inr=usd_inr)

    accounts = []
    for provider in registry.providers.values():
        record = records.get(provider.id)
        state = runtime.describe(provider, record)
        share = typical["by_provider"].get(provider.id, 0.0)  # rupees of a full-length call
        balance = None
        kind = "none"
        spent = None
        as_of = None
        if state["linked"]:
            if provider.balance == "api":
                live = await usage.live_balance(provider.id)
                if live is not None:
                    balance, kind = _money(live, provider.currency, usd_inr), "live"
            saved = (record or {}).get("balance") or {}
            if balance is None and saved.get("amount") is not None:
                since = usage.parse_time(saved.get("as_of"))
                spent_inr = usage.spent_since(rows, costs, provider.id, since)
                spent_own = spent_inr / usd_inr if provider.currency == "USD" else spent_inr
                balance = _money(max(float(saved["amount"]) - spent_own, 0.0), provider.currency, usd_inr)
                spent = _money(spent_own, provider.currency, usd_inr)
                kind, as_of = "estimated", saved.get("as_of")
        calls_left = int(balance["inr"] / share) if balance and share > 0 else None
        low_at = (record or {}).get("low_balance")
        accounts.append({
            "id": provider.id, "name": provider.name, "role": provider.role, "does": provider.does,
            "required_for_calls": provider.required_for_calls, "custom": False, "help": provider.help,
            "currency": provider.currency, "balance_kind": kind, "balance": balance, "spent_since": spent,
            "balance_as_of": as_of, "balance_source": provider.balance,
            "recharge_url": (record or {}).get("recharge_url") or provider.recharge_url,
            "dashboard_url": provider.dashboard_url,
            "per_call": round(share, 2), "calls_left": calls_left,
            "low": calls_left is not None and calls_left < (int(low_at) if low_at else 20),
            "in_use": provider.id in (current_stack().telephony, current_stack().stt, "openai"),
            **state,
        })
    for account_id, record in records.items():
        if account_id in registry.providers or not record.get("custom"):
            continue
        saved = record.get("balance") or {}
        currency = saved.get("currency") or "INR"
        accounts.append({
            "id": account_id, "name": record.get("name") or account_id, "role": record.get("role") or "Other",
            "does": record.get("does") or "", "required_for_calls": False, "custom": True, "help": "",
            "currency": currency, "balance_kind": "manual" if saved.get("amount") is not None else "none",
            "balance": _money(float(saved["amount"]), currency, usd_inr) if saved.get("amount") is not None else None,
            "spent_since": None, "balance_as_of": saved.get("as_of"), "balance_source": "manual",
            "recharge_url": record.get("recharge_url") or "", "dashboard_url": "", "per_call": 0, "calls_left": None,
            "low": False, "in_use": False, "linked": True, "source": "console", "needs_relink": False, "fields": [],
            "can_use_server_keys": False,
        })

    connected = [r for r in rows if r.connected]
    connected_costs = [costs[r.attempt_id]["total"] for r in connected]
    seconds = sum(r.line_seconds for r in connected)
    left = [a["calls_left"] for a in accounts if a["calls_left"] is not None and a["in_use"]]
    return {
        "usd_inr": usd_inr, "usd_inr_default": registry.usd_inr, "accounts": accounts,
        "stack": current_stack().__dict__,
        "per_call": {"seconds": limit, **typical},
        "per_minute": one_minute["total"],
        "actual": {
            "calls": len(rows), "connected": len(connected),
            "total": round(sum(c["total"] for c in costs.values()), 2),
            "avg_per_connected": round(sum(connected_costs) / len(connected_costs), 2) if connected_costs else None,
            "avg_seconds": round(seconds / len(connected)) if connected else None,
            "per_minute": round(sum(connected_costs) / (seconds / 60), 2) if seconds else None,
        },
        "calls_left": min(left) if left else None,
        "problems": runtime.blocking_problems(),
    }


@router.get("/accounts")
async def list_accounts() -> dict:
    return await accounts_overview()


class LinkBody(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)
    use_server_keys: bool = False


@router.post("/accounts/{account_id}/link")
async def link_account(account_id: str, body: LinkBody) -> dict:
    provider = get_registry().providers.get(account_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown account")
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        if body.use_server_keys:
            await runtime.link_server_keys(session, tenant_id, provider)
            message = f"{provider.name} is using the server's own keys again."
        else:
            values = {c.field: (body.values.get(c.field) or "").strip() for c in provider.credentials}
            missing = [c.label for c in provider.credentials if not values[c.field]]
            if missing:
                raise HTTPException(status_code=422, detail=f"Fill in: {', '.join(missing)}")
            if account_id == "vobiz" and not re.fullmatch(r"\+\d{8,15}", values["from_number"].replace(" ", "")):
                raise HTTPException(status_code=422, detail="The calling number must look like +918012345678")
            if account_id == "vobiz":
                values["from_number"] = values["from_number"].replace(" ", "")
            ok, message = await runtime.verify(provider, values)
            if not ok:
                raise HTTPException(status_code=422, detail=message)
            await runtime.link(session, tenant_id, provider, values)
    usage.forget_balances()
    return {"linked": True, "message": message}


@router.post("/accounts/{account_id}/unlink")
async def unlink_account(account_id: str) -> dict:
    registry = get_registry()
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        provider = registry.providers.get(account_id)
        if provider is None:
            if not await store.delete_value(session, tenant_id, runtime.PREFIX + account_id):
                raise HTTPException(status_code=404, detail="Unknown account")
            return {"linked": False, "removed": True}
        await runtime.unlink(session, tenant_id, provider)
    usage.forget_balances()
    return {"linked": False}


class AccountUpdate(BaseModel):
    balance: float | None = Field(default=None, ge=0, le=10_000_000)
    recharge_url: str | None = Field(default=None, max_length=300)
    low_balance: int | None = Field(default=None, ge=0, le=100_000)
    name: str | None = Field(default=None, max_length=60)
    role: str | None = Field(default=None, max_length=60)


def _checked_url(url: str) -> str:
    url = url.strip()
    if url and not re.match(r"^https://[^\s<>\"']+$", url):
        raise HTTPException(status_code=422, detail="The recharge link must start with https://")
    return url


@router.put("/accounts/{account_id}")
async def update_account(account_id: str, body: AccountUpdate) -> dict:
    """Set the balance to track (after a recharge), the recharge link, or the
    low-balance warning level."""
    registry = get_registry()
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        provider = registry.providers.get(account_id)
        record = await store.get_value(session, tenant_id, runtime.PREFIX + account_id)
        if provider is None and not (record or {}).get("custom"):
            raise HTTPException(status_code=404, detail="Unknown account")
        changes: dict = {}
        if body.balance is not None:
            currency = provider.currency if provider else ((record or {}).get("balance") or {}).get("currency", "INR")
            changes["balance"] = {"amount": body.balance, "currency": currency,
                                  "as_of": datetime.now(timezone.utc).isoformat()}
        if body.recharge_url is not None:
            changes["recharge_url"] = _checked_url(body.recharge_url)
        if body.low_balance is not None:
            changes["low_balance"] = body.low_balance
        if provider is None:
            if body.name:
                changes["name"] = body.name.strip()
            if body.role is not None:
                changes["role"] = body.role.strip()
        await runtime.update_record(session, tenant_id, account_id, changes)
    return {"saved": True}


class CustomAccount(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(default="", max_length=60)
    recharge_url: str = Field(default="", max_length=300)
    balance: float | None = Field(default=None, ge=0, le=10_000_000)
    currency: str = "INR"


@router.post("/accounts")
async def add_custom_account(body: CustomAccount) -> dict:
    """Another service the client wants on this page (a name, a recharge link, a
    balance they keep up to date themselves)."""
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        records = await runtime.load_records(session, tenant_id)
        base = "other-" + (re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:40] or "account")
        account_id, n = base, 2
        while account_id in records or account_id in get_registry().providers:
            account_id, n = f"{base}-{n}", n + 1
        record = {"custom": True, "linked": True, "name": body.name.strip(), "role": body.role.strip(),
                  "recharge_url": _checked_url(body.recharge_url)}
        if body.balance is not None:
            record["balance"] = {"amount": body.balance, "currency": "USD" if body.currency == "USD" else "INR",
                                 "as_of": datetime.now(timezone.utc).isoformat()}
        await store.set_value(session, tenant_id, runtime.PREFIX + account_id, record)
    return {"id": account_id}


class AccountSettings(BaseModel):
    usd_inr: float = Field(ge=50, le=200)


@router.put("/account-settings")
async def save_account_settings(body: AccountSettings) -> dict:
    async with session_scope() as session:
        tenant_id = await _tenant_id(session)
        await store.set_value(session, tenant_id, runtime.SETTINGS_KEY, {"usd_inr": body.usd_inr})
    return {"usd_inr": body.usd_inr}
