"""Tiny recurrence format. Not cron on purpose: 'daily@HH:MM' (local tz) and 'every:<N>(s|m|h)'."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

_EVERY = re.compile(r"^every:(\d+)([smh])$")
_DAILY = re.compile(r"^daily@(\d{1,2}):(\d{2})$")
_UNIT = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([smhd])", text.strip().lower())
    if not m:
        raise ValueError(f"bad duration {text!r} (use e.g. 30m, 2h, 1d)")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(seconds=n * (_UNIT.get(unit) or 86400))


def next_run(recurrence: str, after: datetime, tz: str = "UTC") -> datetime:
    """First run strictly after `after` for the given recurrence."""
    m = _EVERY.match(recurrence)
    if m:
        return after + timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2)])
    m = _DAILY.match(recurrence)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        zone = ZoneInfo(tz)
        local = after.astimezone(zone)
        candidate = datetime.combine(local.date(), time(hh, mm), tzinfo=zone)
        if candidate <= local:
            candidate = datetime.combine(local.date() + timedelta(days=1), time(hh, mm), tzinfo=zone)
        return candidate.astimezone(after.tzinfo or zone)
    raise ValueError(f"unknown recurrence {recurrence!r}")


def validate(recurrence: str) -> None:
    if not (_EVERY.match(recurrence) or _DAILY.match(recurrence)):
        raise ValueError(f"unknown recurrence {recurrence!r}")
