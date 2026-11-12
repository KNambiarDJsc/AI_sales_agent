"""Call scripts: the *content* of a call, separate from its *flow*.

- The flow (config/scripts/*.yaml → ScriptConfig) is the state machine and its
  guardrails: which states exist, which tools each allows, do-not-call handling.
- A call script (this module) is what the client wants said: who the agent is, the
  first line, the pitch, and the facts she may state. Built-in ones are YAML files in
  config/call_scripts/; the client's own uploads (.txt/.docx/.pdf) are stored in the
  database in the same shape (services/scripts/store.py).

Nothing here talks to the database or an LLM. A call script never changes what the
application enforces (CLAUDE.md rules 2 and 3) — it only fills the prompt and the
opening line of a flow that declares `uses_call_script: true`.
"""
from __future__ import annotations

import re
import string
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from config.settings import REPO_ROOT
from orchestrator.state_machine import ScriptConfig, StateConfig

CALL_SCRIPTS_DIR = REPO_ROOT / "config" / "call_scripts"
DEFAULT_CALL_SCRIPT_ID = "amazon-fresh-seller"

LanguageStyle = Literal["hinglish", "hindi", "english"]

# Fields an opening line may use: the two below come from the call script itself, the
# rest from the lead sheet (services/lead_import: known columns are stored under
# these names). A line using a field the lead doesn't have is skipped, never spoken
# with a blank in it (ScriptConfig.opening_line_for).
SCRIPT_FIELDS = ("agent_name", "company_name")
LEAD_FIELDS = ("contact_name", "business_name", "product_category", "brand", "email", "city")
OPENING_FIELDS = SCRIPT_FIELDS + LEAD_FIELDS

MAX_SCRIPT_CHARS = 12_000  # ~3,000 tokens: every turn of every call carries the script
_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-")[:60] or "script"


def placeholders(text: str) -> set[str]:
    try:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}
    except ValueError:  # unbalanced braces
        return {"<invalid>"}


class CallScript(BaseModel):
    id: str
    name: str
    description: str = ""
    language_style: LanguageStyle = "hinglish"
    agent_name: str = ""
    company_name: str = ""
    opening_lines: list[str] = Field(default_factory=list)
    script_text: str = ""
    knowledge: dict[str, str] = Field(default_factory=dict)
    objection_playbook: dict[str, str] = Field(default_factory=dict)
    product_info: str = ""
    target_customer: str = ""
    source: Literal["builtin", "uploaded", "written"] = "builtin"
    source_filename: str = ""

    @field_validator("opening_lines")
    @classmethod
    def _known_fields_only(cls, lines: list[str]) -> list[str]:
        cleaned = []
        for line in (str(x).strip() for x in lines):
            if not line:
                continue
            unknown = placeholders(line) - set(OPENING_FIELDS)
            if unknown:
                raise ValueError(
                    f"The first line uses {{{', '.join(sorted(unknown))}}}, which isn't a known field. "
                    f"Use only: {', '.join('{' + f + '}' for f in OPENING_FIELDS)}."
                )
            cleaned.append(line)
        return cleaned

    @field_validator("script_text")
    @classmethod
    def _bounded(cls, text: str) -> str:
        text = text.strip()
        if len(text) > MAX_SCRIPT_CHARS:
            raise ValueError(
                f"The script is {len(text):,} characters; the limit is {MAX_SCRIPT_CHARS:,}. "
                "Keep only what the agent should say on the call."
            )
        return text


@lru_cache
def builtin_call_scripts() -> dict[str, CallScript]:
    scripts: dict[str, CallScript] = {}
    for path in sorted(CALL_SCRIPTS_DIR.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        script = CallScript(**{**raw, "source": "builtin"})
        scripts[script.id] = script
    return scripts


def default_call_script() -> CallScript:
    scripts = builtin_call_scripts()
    return scripts.get(DEFAULT_CALL_SCRIPT_ID) or next(iter(scripts.values()))


def script_fields(call_script: CallScript) -> dict[str, str]:
    """The call script's own values for `{agent_name}` / `{company_name}`."""
    return {"agent_name": call_script.agent_name.strip(), "company_name": call_script.company_name.strip()}


def apply_to_flow(flow: ScriptConfig, call_script: CallScript) -> ScriptConfig:
    """The flow with this call script's first line and language. A copy: the loaded
    flow is shared between calls."""
    english = call_script.language_style == "english"
    states = dict(flow.states)
    start = states.get("INTRO")
    if start is not None and call_script.opening_lines:
        states["INTRO"] = StateConfig(**{**start.model_dump(), "opening_line": list(call_script.opening_lines)})
    return flow.model_copy(update={
        "states": states,
        "language": "en-IN" if english else "hi-IN",
        "speech_style": call_script.language_style,
    })


def campaign_prompt_from(call_script: CallScript) -> dict:
    """The prompt inputs orchestrator/prompts.py reads (same keys as a
    config/prompts/<campaign>.yaml file, plus the script itself)."""
    return {
        "campaign_id": call_script.id,
        "product_info": call_script.product_info,
        "target_customer": call_script.target_customer,
        "knowledge": dict(call_script.knowledge),
        "objection_playbook": dict(call_script.objection_playbook),
        "call_script": call_script.script_text,
        "call_script_name": call_script.name,
        "agent_name": call_script.agent_name.strip(),
        "company_name": call_script.company_name.strip(),
    }
