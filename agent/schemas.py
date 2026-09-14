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
    proposed_start: str = Field(default="", description="when the event asks the person to meet, attend or be somewhere at a "
                                "clock time: that start, ISO-8601 with the offset of their time zone; empty otherwise")
    proposed_end: str = Field(default="", description="that end when the text gives one, same format; empty otherwise")
    reply: str = Field(default="", max_length=800, description="only with a calendar section: the reply the person could send")


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


class LearnResult(BaseModel):
    """What the agent proposes to remember after sampling a freshly connected service."""

    facts: list[str] = Field(default_factory=list, max_length=8, description="3-6 durable facts about the person, third person, one sentence each")
    profile_suggestion: str = Field(default="", max_length=400, description="1-3 sentences: who they are, what matters, what is urgent")
    top_people: list[str] = Field(default_factory=list, max_length=6, description="names (with role if obvious) the person deals with most")
    message: str = Field(max_length=900, description="what to say to the person: what you noticed, ask them to confirm or correct")
