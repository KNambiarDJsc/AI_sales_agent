"""Core schema (Section 15 of the architecture spec).

Deliberately un-opinionated about business rules: qualification outcomes, script
content, and campaign config live in JSONB/config files, not as rigid columns, so
adding a new campaign type or qualification dimension doesn't require a migration.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base, TimestampMixin, UUIDPKMixin


class Tenant(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class User(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "user"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="sales_rep", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Campaign(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "campaign"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    script_id: Mapped[str] = mapped_column(String(100), nullable=False)
    script_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)  # draft/active/paused/completed
    telephony_provider: Mapped[str] = mapped_column(String(30), nullable=False)
    call_window_start: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    call_window_end: Mapped[str] = mapped_column(String(5), nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="Asia/Kolkata")
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_call_duration_seconds: Mapped[int] = mapped_column(Integer, default=420, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    leads: Mapped[list["Lead"]] = relationship(back_populates="campaign")


class Lead(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "lead"
    __table_args__ = (UniqueConstraint("campaign_id", "dedupe_key", name="uq_lead_campaign_dedupe"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), nullable=False, index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaign.id"), nullable=False, index=True)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    raw_phone: Mapped[str] = mapped_column(String(50), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(20), nullable=False)  # normalized phone, or client-specified key
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # arbitrary uploaded columns
    status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )  # pending/queued/in_progress/completed/failed/suppressed
    attempts_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consent_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source_row: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    campaign: Mapped[Campaign] = relationship(back_populates="leads")


class CallAttempt(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "call_attempt"
    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id", "attempt_number", name="uq_call_attempt_idempotency"),
        Index("ix_call_attempt_provider_call_id", "provider_call_id"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaign.id"), nullable=False, index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lead.id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False, index=True)
    # queued/dialing/ringing/in_progress/completed/failed/no_answer/busy/canceled
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Conversation(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "conversation"

    call_attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("call_attempt.id"), nullable=False, index=True)
    script_id: Mapped[str] = mapped_column(String(100), nullable=False)
    script_version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_state: Mapped[str] = mapped_column(String(50), nullable=False, default="INTRO")
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Turn(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "turn"
    __table_args__ = (UniqueConstraint("conversation_id", "turn_index", name="uq_turn_index"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversation.id"), nullable=False, index=True)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str] = mapped_column(String(20), nullable=False)  # agent | customer
    state: Mapped[str] = mapped_column(String(50), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_llm_output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class TranscriptSegment(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "transcript_segment"

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversation.id"), nullable=False, index=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("turn.id"), nullable=True, index=True)
    speaker: Mapped[str] = mapped_column(String(20), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_final: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stt_confidence: Mapped[float | None] = mapped_column(nullable=True)


class Qualification(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "qualification"

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversation.id"), nullable=False, index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lead.id"), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    qualified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.0)
    dimensions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # need/fit/timing/authority/...
    facts: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # extracted structured facts
    objections: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # transcript turn refs/quotes
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(255), nullable=True)
    script_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sales_followup_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Callback(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "callback"

    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lead.id"), nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversation.id"), nullable=True)
    requested_time: Mapped[datetime | None] = mapped_column(nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Kolkata", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)  # pending/completed/canceled
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Suppression(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "suppression"
    __table_args__ = (UniqueConstraint("tenant_id", "phone_e164", name="uq_suppression_tenant_phone"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), nullable=False, index=True)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)  # dnc_request/manual/complaint/etc
    source: Mapped[str] = mapped_column(String(100), nullable=False)  # call_id / user / import


class AgentConfig(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "agent_config"
    __table_args__ = (UniqueConstraint("tenant_id", "key", "version", name="uq_agent_config_key_version"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ScriptVersion(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "script_version"
    __table_args__ = (UniqueConstraint("script_id", "version", name="uq_script_id_version"),)

    script_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)  # config/scripts/<file>.yaml
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ModelVersion(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "model_version"

    role: Mapped[str] = mapped_column(String(20), nullable=False)  # stt | tts | llm
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class OutboxEvent(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "outbox_event"

    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)


class AuditLog(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "audit_log"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenant.id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class ExportJob(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "export_job"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), nullable=False, index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaign.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)  # pending/running/done/failed
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
