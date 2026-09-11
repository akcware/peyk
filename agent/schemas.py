"""Pydantic contracts shared between agent/ and workers/. No I/O, no SDK imports: workers import this
without pulling strands/boto3, and the agent package stays DB-free."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["person", "transactional", "newsletter", "automated", "calendar", "other"]


class TriageResult(BaseModel):
    urgency: int = Field(ge=1, le=5, description="1 = ignore, 5 = interrupt now")
    category: Category
    reason: str = Field(max_length=200, description="one short internal sentence: why this urgency")
    summary: str = Field(default="", max_length=320,
                         description="what a good assistant would tell the person: who, what, what is needed, by when")


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


class ChatAck(BaseModel):
    """The assistant's first reflex to a message: either a direct short answer, or a natural one-liner
    saying what it is about to look at (then the full agent runs)."""

    needs_work: bool = Field(description="true if answering requires looking at mail/calendar/memory, drafting, scheduling")
    message: str = Field(max_length=300, description="what to say right now, in the user's language")
