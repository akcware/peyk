"""Agent tools. Read tools return data; side-effect tools return *intents* (plain dicts) that workers
apply. The agent itself never writes to the DB or sends anything."""
from __future__ import annotations

from datetime import datetime

from strands import tool


@tool
def schedule_followup(when_iso: str, note: str) -> dict:
    """Schedule a reminder for the user at a specific time.

    Args:
        when_iso: ISO-8601 timestamp with timezone, e.g. 2026-09-11T09:00:00+02:00
        note: what to remind the user about (one sentence)
    """
    datetime.fromisoformat(when_iso)  # validate early; raises for garbage
    return {"intent": "ScheduleRequest", "when_iso": when_iso, "note": note}


@tool
def remember(text: str) -> dict:
    """Store a fact about the user for later (phase 3).

    Args:
        text: the fact to remember, in one sentence
    """
    return {"intent": "MemoryWrite", "text": text}
