"""Where call scripts live and which one a call uses.

Built-in scripts are files (config/call_scripts/). The client's own scripts are rows in
`agent_config` (key "call_script:<id>"), one per script, in the workspace (tenant) the
console works in. Which script calls use is one more row ("active_call_script").
A built-in script can be edited from the console: the edit is saved as a row with the
same id, which then takes its place (the file is the "reset to original").
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AgentConfig, Campaign
from orchestrator.call_script import (
    CallScript,
    apply_to_flow,
    builtin_call_scripts,
    campaign_prompt_from,
    default_call_script,
    script_fields,
    slugify,
)
from orchestrator.prompts import load_campaign_prompt
from orchestrator.state_machine import ScriptConfig, load_script_by_id
from config.settings import PROMPTS_DIR

_PREFIX = "call_script:"
_ACTIVE_KEY = "active_call_script"


async def _row(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> AgentConfig | None:
    return (await session.execute(
        select(AgentConfig).where(AgentConfig.tenant_id == tenant_id, AgentConfig.key == key, AgentConfig.version == 1)
    )).scalar_one_or_none()


async def get_value(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> dict | None:
    row = await _row(session, tenant_id, key)
    return dict(row.value) if row is not None and row.is_active else None


async def set_value(session: AsyncSession, tenant_id: uuid.UUID, key: str, value: dict) -> None:
    row = await _row(session, tenant_id, key)
    if row is None:
        session.add(AgentConfig(tenant_id=tenant_id, key=key, version=1, value=value, is_active=True))
    else:
        row.value, row.is_active = value, True
    await session.flush()


async def delete_value(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> bool:
    row = await _row(session, tenant_id, key)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


def _from_row(value: dict) -> CallScript | None:
    try:
        return CallScript(**value)
    except Exception:  # noqa: BLE001 - a row from an older shape must not break calls
        return None


async def list_scripts(session: AsyncSession, tenant_id: uuid.UUID) -> list[CallScript]:
    """Every script the console can use: the client's own (newest first), then the
    built-in ones (an edited built-in appears once, as the edit)."""
    rows = (await session.execute(
        select(AgentConfig).where(AgentConfig.tenant_id == tenant_id, AgentConfig.key.like(f"{_PREFIX}%"),
                                  AgentConfig.is_active.is_(True)).order_by(AgentConfig.updated_at.desc())
    )).scalars().all()
    own = [s for s in (_from_row(r.value) for r in rows) if s is not None]
    seen = {s.id for s in own}
    return own + [s for s in builtin_call_scripts().values() if s.id not in seen]


async def get_script(session: AsyncSession, tenant_id: uuid.UUID, script_id: str) -> CallScript | None:
    value = await get_value(session, tenant_id, _PREFIX + script_id)
    if value is not None:
        found = _from_row(value)
        if found is not None:
            return found
    return builtin_call_scripts().get(script_id)


async def save_script(session: AsyncSession, tenant_id: uuid.UUID, script: CallScript) -> CallScript:
    await set_value(session, tenant_id, _PREFIX + script.id, script.model_dump())
    return script


async def new_script_id(session: AsyncSession, tenant_id: uuid.UUID, name: str) -> str:
    base = slugify(name)
    taken = {s.id for s in await list_scripts(session, tenant_id)}
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


async def delete_script(session: AsyncSession, tenant_id: uuid.UUID, script_id: str) -> bool:
    """Removes the client's script (or their edit of a built-in one, which brings the
    original back). If it was the one in use, calls go back to the default."""
    removed = await delete_value(session, tenant_id, _PREFIX + script_id)
    if removed and script_id not in builtin_call_scripts():
        active = await get_value(session, tenant_id, _ACTIVE_KEY)
        if active and active.get("id") == script_id:
            await delete_value(session, tenant_id, _ACTIVE_KEY)
    return removed


async def active_script_id(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    active = await get_value(session, tenant_id, _ACTIVE_KEY)
    if active and await get_script(session, tenant_id, str(active.get("id"))) is not None:
        return str(active["id"])
    return default_call_script().id


async def set_active_script(session: AsyncSession, tenant_id: uuid.UUID, script_id: str) -> None:
    await set_value(session, tenant_id, _ACTIVE_KEY, {"id": script_id})


async def call_script_for(
    session: AsyncSession, campaign: Campaign, *, tenant_id: uuid.UUID | None = None, script_id: str | None = None
) -> CallScript:
    """The call script this campaign's next call uses: an explicit `script_id`, else
    the campaign's own choice (`config.call_script_id`, e.g. a test call trying a
    draft), else the one marked in use, else the default. `tenant_id` is the
    workspace whose scripts to use when it isn't the campaign's own (the browser
    demo previews the console's scripts)."""
    tenant = tenant_id or campaign.tenant_id
    for chosen in (script_id, (campaign.config or {}).get("call_script_id")):
        if chosen:
            found = await get_script(session, tenant, str(chosen))
            if found is not None:
                return found
    found = await get_script(session, tenant, await active_script_id(session, tenant))
    return found or default_call_script()


async def flow_for_campaign(
    session: AsyncSession, campaign: Campaign, *, tenant_id: uuid.UUID | None = None, script_id: str | None = None
) -> tuple[ScriptConfig, dict, dict]:
    """(flow, campaign prompt, script fields) for a call in this campaign.

    A flow that takes its content from a call script (`uses_call_script`) gets the
    script's first line, language style and prompt; `script fields` are the values for
    `{agent_name}` / `{company_name}`, to merge into the lead's fields. Any other flow
    works as before: its prompt file, no extra fields."""
    flow = load_script_by_id(campaign.script_id)
    if not flow.uses_call_script:
        path = (campaign.config or {}).get("campaign_prompt_path") or (PROMPTS_DIR / "campaign_prompt_template.yaml")
        return flow, load_campaign_prompt(path), {}
    call_script = await call_script_for(session, campaign, tenant_id=tenant_id, script_id=script_id)
    return apply_to_flow(flow, call_script), campaign_prompt_from(call_script), script_fields(call_script)
