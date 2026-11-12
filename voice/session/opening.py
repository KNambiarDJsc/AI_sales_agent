"""Record a lead's scripted opening line while their phone is still ringing.

Synthesizing the greeting ("नमस्ते, क्या मेरी बात {name} जी से…") only after pickup
left ~1.5-2 s of silence on a live Vobiz call — long enough for people to say "hello?"
twice or hang up. Ringing takes several seconds, so the line is recorded then
(acknowledgments.prerecord_one_off) and `VoiceSession` plays the recording at once.
Best effort: if it isn't ready in time the line is synthesized as before.
"""
from __future__ import annotations

import asyncio
import logging

from orchestrator.state_machine import ScriptConfig, StateMachine
from speech.tts.factory import get_tts_provider
from voice.session import acknowledgments

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


# Lead-sheet column headers → the field names scripts use ({brand}, {email}, ...).
# Columns not listed keep their own header and are still shown to the agent.
_FIELD_ALIASES = {
    "product_category": {"product category", "category", "product", "products", "product type", "what they sell"},
    "brand": {"brand", "brand name", "brands"},
    "email": {"email", "e-mail", "email id", "mail", "mail id", "registered email", "email address"},
    "city": {"city", "town", "location"},
    "gst_number": {"gst", "gstin", "gst number", "gst no"},
}
_ALIAS_TO_FIELD = {alias: field for field, aliases in _FIELD_ALIASES.items() for alias in aliases}


def lead_fields(contact_name: str | None, business_name: str | None, extra: dict | None) -> dict:
    """The fields the script's lines are filled from — the same dict the call uses.
    Empty cells are left out, so a line that needs them is skipped instead of being
    spoken with a blank (ScriptConfig.opening_line_for)."""
    fields: dict = {"contact_name": contact_name, "business_name": business_name}
    for key, value in (extra or {}).items():
        text = str(value).strip() if value is not None else ""
        if not text or text.lower() in ("nan", "none", "null", "-"):
            continue
        name = str(key).strip()
        fields[_ALIAS_TO_FIELD.get(name.lower().replace("_", " "), name)] = text
    return fields


def prerecord_opening(flow: ScriptConfig, fields: dict) -> None:
    try:
        text = StateMachine(flow, current_state="INTRO").opening_line(fields)
    except Exception:  # noqa: BLE001 - never block dialling over this
        logger.exception("opening_prerecord_failed")
        return
    if not text:
        return
    task = asyncio.create_task(acknowledgments.prerecord_one_off(get_tts_provider(), text))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
