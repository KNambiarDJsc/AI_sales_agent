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

from orchestrator.state_machine import StateMachine, load_script_by_id
from speech.tts.factory import get_tts_provider
from voice.session import acknowledgments

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def lead_fields(contact_name: str | None, business_name: str | None, extra: dict | None) -> dict:
    """The fields the script's lines are filled from — the same dict the call uses."""
    return {"contact_name": contact_name, "business_name": business_name, **(extra or {})}


def prerecord_opening(script_id: str, fields: dict) -> None:
    try:
        text = StateMachine(load_script_by_id(script_id), current_state="INTRO").opening_line(fields)
    except Exception:  # noqa: BLE001 - never block dialling over this
        logger.exception("opening_prerecord_failed")
        return
    if not text:
        return
    task = asyncio.create_task(acknowledgments.prerecord_one_off(get_tts_provider(), text))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
