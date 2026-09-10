"""Domain models. Pydantic, no ORM. Column names mirror db/migrations/0001_core.sql."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Source = Literal["gmail", "telegram", "calendar", "whatsapp", "system"]
Kind = Literal["message_in", "message_out", "event_starting", "event_created", "tick"]
ObservationStatus = Literal["new", "claimed", "done", "failed"]


class Observation(BaseModel):
    """One thing that happened in the world, as seen by an adapter. Immutable facts + queue state."""

    id: UUID | None = None
    user_id: UUID
    source: str
    source_key: str
    kind: str
    occurred_at: datetime
    received_at: datetime | None = None
    thread_key: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    is_backfill: bool = False
    status: ObservationStatus = "new"
    claimed_at: datetime | None = None
    attempts: int = 0


class Connection(BaseModel):
    """Opaque per-adapter connection handle. Adapters put whatever they need in `data`."""

    adapter_id: str
    user_id: UUID
    data: dict[str, Any] = Field(default_factory=dict)


class Content(BaseModel):
    """Outbound message content, channel-agnostic. Adapters render it."""

    text: str
    subject: str | None = None
    to: list[str] = Field(default_factory=list)
    reply_markup: dict[str, Any] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Person(BaseModel):
    id: UUID | None = None
    user_id: UUID
    display_name: str | None = None


class Identity(BaseModel):
    id: UUID | None = None
    user_id: UUID
    person_id: UUID
    kind: Literal["email", "phone", "tg_user_id", "slack_uid"]
    value: str
    confidence: float = 1.0
