from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import CallAttempt, Conversation, Qualification, Turn


class CallRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_attempt(self, attempt: CallAttempt) -> CallAttempt:
        """Idempotent on (campaign_id, lead_id, attempt_number) via the DB unique
        constraint — callers should catch IntegrityError and treat as "already exists"
        rather than retry-creating (Section 19)."""
        self.session.add(attempt)
        await self.session.flush()
        return attempt

    async def get_attempt_by_provider_call_id(self, provider_call_id: str) -> CallAttempt | None:
        result = await self.session.execute(
            select(CallAttempt).where(CallAttempt.provider_call_id == provider_call_id)
        )
        return result.scalar_one_or_none()

    async def mark_attempt_status(self, attempt_id: uuid.UUID, status: str, **fields) -> None:
        attempt = await self.session.get(CallAttempt, attempt_id)
        if attempt is None:
            return
        attempt.status = status
        for key, value in fields.items():
            setattr(attempt, key, value)
        await self.session.flush()

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def append_turn(self, turn: Turn) -> Turn:
        self.session.add(turn)
        await self.session.flush()
        return turn

    async def close_conversation(self, conversation_id: uuid.UUID, reason: str) -> None:
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation is None:
            return
        conversation.ended_at = datetime.now(timezone.utc)
        conversation.ended_reason = reason
        await self.session.flush()

    async def save_qualification(self, qualification: Qualification) -> Qualification:
        self.session.add(qualification)
        await self.session.flush()
        return qualification
