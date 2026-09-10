"""Pydantic contracts shared between agent/ and workers/. No I/O, no SDK imports: workers import this
without pulling strands/boto3, and the agent package stays DB-free."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["person", "transactional", "newsletter", "automated", "calendar", "other"]


class TriageResult(BaseModel):
    urgency: int = Field(ge=1, le=5, description="1 = ignore, 5 = interrupt now")
    category: Category
    reason: str = Field(max_length=200)


class ScheduleRequest(BaseModel):
    intent: Literal["ScheduleRequest"] = "ScheduleRequest"
    when_iso: str
    note: str


class MemoryWrite(BaseModel):
    intent: Literal["MemoryWrite"] = "MemoryWrite"
    text: str


class ActionDraft(BaseModel):
    intent: Literal["ActionDraft"] = "ActionDraft"
    channel: str = "gmail"
    thread_key: str | None = None
    to: list[str] = []
    subject: str | None = None
    body: str


class NeedMore(BaseModel):
    intent: Literal["NeedMore"] = "NeedMore"
    query: str
    since_days: int = 7


class ChatResponse(BaseModel):
    reply: str
    intents: list[dict] = []
