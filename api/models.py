"""
Pydantic models matching life_radar schema in PostgreSQL.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


# --- Enums ---
class ConversationState(str):
    ACTIVE = "active"
    ARCHIVED = "archived"
    MUTED = "muted"


class CommitmentOwnerRole(str):
    USER = "user"
    OTHER = "other"
    SHARED = "shared"
    ASSISTANT = "assistant"


class CommitmentStatus(str):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class ReminderStatus(str):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    SENT = "sent"
    SNOOZED = "snoozed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class PlannedActionStatus(str):
    PROPOSED = "proposed"
    SCHEDULED = "scheduled"
    READY = "ready"
    DONE = "done"
    CANCELLED = "cancelled"


class MemoryKind(str):
    FACT = "fact"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    SKILL = "skill"


class MemorySensitivity(str):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    RESTRICTED = "restricted"


class DraftStatus(str):
    READY = "ready"
    NEEDS_CONTEXT = "needs_context"
    APPROVED = "approved"
    SENT = "sent"
    DISCARDED = "discarded"


class FeedbackType(str):
    EXPLICIT = "explicit"
    IMPLICIT = "implicit"


class ProbeStatus(str):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


class SyncState(str):
    ACTIVE = "active"
    PENDING = "pending"
    CONFLICT = "conflict"
    ARCHIVED = "archived"
    DELETED = "deleted"


# --- Base ---
class TimestampedModel(BaseModel):
    created_at: datetime
    updated_at: datetime


# --- Conversations ---
class Conversation(TimestampedModel):
    id: UUID
    source: str
    external_id: str
    account_id: Optional[str] = None
    title: Optional[str] = None
    participants: list = Field(default_factory=list)
    state: str = "active"
    needs_read: bool = False
    needs_reply: bool = False
    important_now: bool = False
    waiting_on_other: bool = False
    follow_up_later: bool = False
    ready_to_act: bool = False
    blocked_needs_context: bool = False
    last_event_at: Optional[datetime] = None
    last_triaged_at: Optional[datetime] = None
    priority_score: Optional[float] = None
    urgency_score: Optional[float] = None
    social_weight: Optional[float] = None
    reward_value: Optional[float] = None
    energy_fit: Optional[float] = None
    effort_estimate_minutes: Optional[int] = None
    due_at: Optional[datetime] = None
    metadata: dict = Field(default_factory=dict)

    class Config:
        from_attributes = True


# --- Message Events ---
class MessageEvent(TimestampedModel):
    id: UUID
    conversation_id: Optional[UUID] = None
    source: str
    external_id: str
    sender_id: Optional[str] = None
    sender_label: Optional[str] = None
    occurred_at: datetime
    content_text: Optional[str] = None
    content_json: dict = Field(default_factory=dict)
    is_inbound: bool = True
    reply_needed: Optional[bool] = None
    needs_read: Optional[bool] = None
    needs_reply: Optional[bool] = None
    importance_score: Optional[float] = None
    triage_summary: Optional[str] = None
    provenance: dict = Field(default_factory=dict)

    class Config:
        from_attributes = True


# --- Commitments ---
class Commitment(TimestampedModel):
    id: UUID
    conversation_id: Optional[UUID] = None
    source_event_id: Optional[UUID] = None
    title: str
    summary: Optional[str] = None
    owner_role: str = "other"
    status: str = "open"
    due_at: Optional[datetime] = None
    importance_score: Optional[float] = None
    urgency_score: Optional[float] = None
    social_weight: Optional[float] = None
    confidence: Optional[float] = None
    metadata: dict = Field(default_factory=dict)

    class Config:
        from_attributes = True


# --- Reminders ---
class Reminder(TimestampedModel):
    id: UUID
    source_entity_type: str
    source_entity_id: Optional[UUID] = None
    title: str
    summary: Optional[str] = None
    status: str = "scheduled"
    remind_at: datetime
    remind_channel: Optional[str] = None
    timing_reason: Optional[str] = None
    cadence_profile: Optional[str] = None
    effort_estimate_minutes: Optional[int] = None
    confidence: Optional[float] = None
    metadata: dict = Field(default_factory=dict)

    class Config:
        from_attributes = True


# --- Planned Actions / Tasks ---
class PlannedAction(TimestampedModel):
    id: UUID
    source_entity_type: str
    source_entity_id: Optional[UUID] = None
    title: str
    summary: Optional[str] = None
    status: str = "proposed"
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    calendar_provider: Optional[str] = None
    calendar_external_id: Optional[str] = None
    effort_estimate_minutes: Optional[int] = None
    reward_value: Optional[float] = None
    energy_fit: Optional[float] = None
    metadata: dict = Field(default_factory=dict)

    class Config:
        from_attributes = True


class CalendarEvent(BaseModel):
    """Clean public API model for calendar events (subset of PlannedAction)."""
    id: UUID
    title: str
    summary: Optional[str] = None
    status: str = "scheduled"
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    calendar_provider: Optional[str] = None
    calendar_external_id: Optional[str] = None
    effort_estimate_minutes: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TaskCreate(BaseModel):
    """Schema for creating a new task (planned action)."""
    source_entity_type: str
    title: str
    summary: Optional[str] = None
    status: str = "proposed"
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    effort_estimate_minutes: Optional[int] = None


class CalendarEventUpsert(BaseModel):
    """Schema for upserting a calendar event (planned action with calendar_external_id)."""
    title: str
    summary: Optional[str] = None
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    calendar_external_id: Optional[str] = None
    calendar_provider: Optional[str] = None


class MessageSendRequest(BaseModel):
    """Schema for sending a message via Matrix/Outlook."""
    conversation_id: UUID
    content_text: str


class MessageSendResponse(BaseModel):
    """Response for message send endpoint."""
    status: str
    message_id: str


# --- Memory Records ---
class MemoryRecord(TimestampedModel):
    id: UUID
    kind: str
    subject_type: str
    subject_key: str
    title: str
    summary: Optional[str] = None
    detail: Optional[str] = None
    sensitivity: str = "normal"
    confidence: Optional[float] = None
    active: bool = True
    source_event_id: Optional[UUID] = None
    provenance: dict = Field(default_factory=dict)

    class Config:
        from_attributes = True


# --- Probe Status ---
class RuntimeProbe(BaseModel):
    id: UUID
    candidate_id: str
    candidate_type: str
    status: str = "ok"
    observed_at: datetime
    latency_ms: Optional[int] = None
    freshness_seconds: Optional[int] = None
    total_events: Optional[int] = None
    decrypt_failures: Optional[int] = None
    encrypted_non_text: Optional[int] = None
    running_processes: Optional[int] = None
    metadata: dict = Field(default_factory=dict)
    notes: Optional[str] = None

    class Config:
        from_attributes = True


class MessagingCandidate(BaseModel):
    candidate_id: str
    candidate_type: str
    last_status: str = "ok"
    last_probe_at: datetime
    latest_freshness_seconds: Optional[int] = None
    latest_total_events: Optional[int] = None
    latest_decrypt_failures: Optional[int] = None
    latest_encrypted_non_text: Optional[int] = None
    latest_running_processes: Optional[int] = None
    latest_notes: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    updated_at: datetime

    class Config:
        from_attributes = True


# --- Alerts (derived from conversations needing attention) ---
class Alert(BaseModel):
    conversation_id: UUID
    title: str
    alert_type: str  # "needs_reply", "needs_read", "important", "overdue", "blocked"
    priority_score: float
    urgency_score: Optional[float] = None
    due_at: Optional[datetime] = None
    source: str


# --- Health ---
class HealthResponse(BaseModel):
    status: str = "ok"
    database: str = "connected"
    version: str = "1.0.0"
