"""Control-channel commands (Telegram text starting with '/'). Pure parsing + a small apply step."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.recurrence import parse_duration


@dataclass(frozen=True)
class Remind:
    run_at: datetime
    note: str


def parse_remind(text: str, now: datetime, tz: str) -> Remind:
    """/remind 1h note | /remind 09:30 note | /remind tomorrow note | /remind tomorrow 09:30 note"""
    m = re.match(r"^/remind(?:@\w+)?\s+(.+)$", text.strip(), re.DOTALL)
    if not m:
        raise ValueError("usage: /remind <1h|09:30|tomorrow [09:30]> <note>")
    rest = m.group(1).strip()
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)

    dur = re.match(r"^(\d+\s*[smhd])\s+(.+)$", rest, re.DOTALL)
    if dur:
        return Remind(now + parse_duration(dur.group(1)), dur.group(2).strip())

    clock = re.match(r"^(\d{1,2}):(\d{2})\s+(.+)$", rest, re.DOTALL)
    if clock:
        hh, mm = int(clock.group(1)), int(clock.group(2))
        cand = datetime.combine(local_now.date(), time(hh, mm), tzinfo=zone)
        if cand <= local_now:
            cand += timedelta(days=1)
        return Remind(cand.astimezone(UTC), clock.group(3).strip())

    tomorrow = re.match(r"^tomorrow(?:\s+(\d{1,2}):(\d{2}))?\s+(.+)$", rest, re.DOTALL | re.IGNORECASE)
    if tomorrow:
        hh = int(tomorrow.group(1)) if tomorrow.group(1) else 9
        mm = int(tomorrow.group(2)) if tomorrow.group(2) else 0
        cand = datetime.combine(local_now.date() + timedelta(days=1), time(hh, mm), tzinfo=zone)
        return Remind(cand.astimezone(UTC), tomorrow.group(3).strip())

    raise ValueError("usage: /remind <1h|09:30|tomorrow [09:30]> <note>")


def is_command(text: str | None) -> bool:
    return bool(text) and text.startswith("/")


def is_remind(text: str | None) -> bool:
    """The only real command. /start and anything else starting with '/' is handled by the agent as chat."""
    return bool(text) and text.split()[0].split("@")[0] == "/remind"


START_TEXT = "[the person just opened the chat by pressing Start]"


def as_chat_text(text: str | None) -> str:
    """Turn a Telegram command into something the agent can react to naturally."""
    if not text:
        return ""
    head = text.split()[0].split("@")[0]
    if head == "/start":
        return START_TEXT
    if text.startswith("/"):
        return text.lstrip("/")
    return text
