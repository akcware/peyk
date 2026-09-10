"""Interruption budget gate. Pure function, no I/O, no LLM. The model decides *important?*,
this decides *worth interrupting?*. Rule order (first match wins):

  muted                              -> no   (muted_sender)
  urgency >= bypass_urgency          -> yes  (urgency_bypass) — pierces cooldown and quiet hours,
                                             but NOT the daily quota (quota is the hard ceiling)
  quiet hours                        -> no   (quiet_hours)
  urgency <= 2                       -> no   (below_threshold)
  thread cooldown                    -> no   (thread_cooldown)
  sent_today >= daily_quota          -> no   (quota_exhausted)
  otherwise                          -> yes  (ok)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from agent.schemas import TriageResult
from core.identity import normalize_email
from core.models import Observation

Reason = Literal[
    "quota_exhausted", "thread_cooldown", "muted_sender", "quiet_hours",
    "urgency_bypass", "ok", "below_threshold",
]


@dataclass(frozen=True)
class MuteRule:
    kind: Literal["thread", "sender", "category"]
    value: str
    until: datetime | None = None


@dataclass(frozen=True)
class BudgetSettings:
    daily_quota: int = 5
    thread_cooldown_minutes: int = 240
    quiet_hours: tuple[int, int] | None = None   # (start_hour, end_hour), end exclusive, may wrap midnight
    bypass_urgency: int = 5


@dataclass
class GateState:
    sent_today: int
    last_sent_in_thread_at: datetime | None
    mutes: list[MuteRule]
    settings: BudgetSettings
    now: datetime
    sender: str | None = None            # normalized sender identity (email / phone) if known
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    notify: bool
    reason: Reason


def in_quiet_hours(hour: int, window: tuple[int, int] | None) -> bool:
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # wraps midnight, e.g. (23, 8)


def is_muted(obs: Observation, triage: TriageResult, state: GateState) -> bool:
    sender = state.sender
    if sender is None and obs.payload.get("from"):
        sender = normalize_email(str(obs.payload["from"]))
    for rule in state.mutes:
        if rule.until is not None and rule.until <= state.now:
            continue
        if rule.kind == "thread" and obs.thread_key and rule.value == obs.thread_key:
            return True
        if rule.kind == "sender" and sender and rule.value.lower() == sender:
            return True
        if rule.kind == "category" and rule.value == triage.category:
            return True
    return False


def decide(obs: Observation, triage: TriageResult, state: GateState) -> Decision:
    s = state.settings
    if is_muted(obs, triage, state):
        return Decision(False, "muted_sender")
    quota_exhausted = state.sent_today >= s.daily_quota
    if triage.urgency >= s.bypass_urgency:
        return Decision(False, "quota_exhausted") if quota_exhausted else Decision(True, "urgency_bypass")
    if in_quiet_hours(state.now.hour, s.quiet_hours):
        return Decision(False, "quiet_hours")
    if triage.urgency <= 2:
        return Decision(False, "below_threshold")
    if state.last_sent_in_thread_at is not None and (
        state.now - state.last_sent_in_thread_at < timedelta(minutes=s.thread_cooldown_minutes)
    ):
        return Decision(False, "thread_cooldown")
    if quota_exhausted:
        return Decision(False, "quota_exhausted")
    return Decision(True, "ok")
