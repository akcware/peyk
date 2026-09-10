"""Pure gate rules: no LLM, no DB, milliseconds."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.schemas import TriageResult
from core.models import Observation
from tests.conftest import USER_ID
from workers.gate import BudgetSettings, Decision, GateState, MuteRule, decide, in_quiet_hours

NOW = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)          # 14:00 — outside default quiet hours
NIGHT = datetime(2026, 9, 10, 23, 30, tzinfo=UTC)       # inside (23, 8)


def obs(sender="mara@example-client.test", thread="t1") -> Observation:
    return Observation(user_id=USER_ID, source="gmail", source_key="k", kind="message_in",
                       occurred_at=NOW, thread_key=thread, payload={"from": f"Mara <{sender}>", "subject": "x"})


def tri(urgency: int, category="person") -> TriageResult:
    return TriageResult(urgency=urgency, category=category, reason="fixture")


def state(**kw) -> GateState:
    base = {"sent_today": 0, "last_sent_in_thread_at": None, "mutes": [],
            "settings": BudgetSettings(daily_quota=5, thread_cooldown_minutes=240, quiet_hours=(23, 8), bypass_urgency=5),
            "now": NOW}
    base.update(kw)
    return GateState(**base)


CASES = [
    # id,                          urgency, state kwargs,                                                     expected
    ("ok_plain",                    3, {},                                                                   Decision(True, "ok")),
    ("ok_urgency4",                 4, {},                                                                   Decision(True, "ok")),
    ("below_threshold_1",           1, {},                                                                   Decision(False, "below_threshold")),
    ("below_threshold_2",           2, {},                                                                   Decision(False, "below_threshold")),
    ("quota_full_u3",               3, {"sent_today": 5},                                                    Decision(False, "quota_exhausted")),
    ("quota_full_u4",               4, {"sent_today": 7},                                                    Decision(False, "quota_exhausted")),
    ("quota_full_u5_still_blocked", 5, {"sent_today": 5},                                                    Decision(False, "quota_exhausted")),
    ("cooldown_u3",                 3, {"last_sent_in_thread_at": NOW - timedelta(minutes=30)},              Decision(False, "thread_cooldown")),
    ("cooldown_u4",                 4, {"last_sent_in_thread_at": NOW - timedelta(minutes=239)},             Decision(False, "thread_cooldown")),
    ("cooldown_expired_u4",         4, {"last_sent_in_thread_at": NOW - timedelta(minutes=241)},             Decision(True, "ok")),
    ("cooldown_u5_bypass",          5, {"last_sent_in_thread_at": NOW - timedelta(minutes=5)},               Decision(True, "urgency_bypass")),
    ("quiet_u3",                    3, {"now": NIGHT},                                                       Decision(False, "quiet_hours")),
    ("quiet_u4",                    4, {"now": NIGHT},                                                       Decision(False, "quiet_hours")),
    ("quiet_u5_bypass",             5, {"now": NIGHT},                                                       Decision(True, "urgency_bypass")),
    ("quiet_u1_is_quiet_first",     1, {"now": NIGHT},                                                       Decision(False, "quiet_hours")),
    ("muted_sender_u5",             5, {"mutes": [MuteRule("sender", "mara@example-client.test")]},         Decision(False, "muted_sender")),
    ("muted_sender_case_insens",    4, {"mutes": [MuteRule("sender", "MARA@Example-Client.test")]},         Decision(False, "muted_sender")),
    ("muted_thread_u4",             4, {"mutes": [MuteRule("thread", "t1")]},                                Decision(False, "muted_sender")),
    ("muted_other_thread_ok",       4, {"mutes": [MuteRule("thread", "t2")]},                                Decision(True, "ok")),
    ("muted_category_u3",           3, {"mutes": [MuteRule("category", "person")]},                          Decision(False, "muted_sender")),
    ("mute_expired_ok",             3, {"mutes": [MuteRule("sender", "mara@example-client.test", NOW - timedelta(days=1))]}, Decision(True, "ok")),
    ("mute_future_until_blocks",    3, {"mutes": [MuteRule("sender", "mara@example-client.test", NOW + timedelta(days=1))]}, Decision(False, "muted_sender")),
    ("bypass_lower_setting_u4",     4, {"settings": BudgetSettings(bypass_urgency=4, quiet_hours=(23, 8)), "now": NIGHT}, Decision(True, "urgency_bypass")),
    ("quota_boundary_4_of_5",       3, {"sent_today": 4},                                                    Decision(True, "ok")),
]


@pytest.mark.parametrize("case_id,urgency,kw,expected", CASES, ids=[c[0] for c in CASES])
def test_gate_rules(case_id, urgency, kw, expected):
    assert decide(obs(), tri(urgency), state(**kw)) == expected


def test_quiet_hours_window():
    assert in_quiet_hours(23, (23, 8)) and in_quiet_hours(3, (23, 8)) and in_quiet_hours(7, (23, 8))
    assert not in_quiet_hours(8, (23, 8)) and not in_quiet_hours(14, (23, 8))
    assert in_quiet_hours(10, (9, 12)) and not in_quiet_hours(12, (9, 12))
    assert not in_quiet_hours(5, None) and not in_quiet_hours(5, (5, 5))


def test_rule_order_muted_beats_bypass_and_quota_beats_bypass():
    s = state(sent_today=99, mutes=[MuteRule("thread", "t1")])
    assert decide(obs(), tri(5), s).reason == "muted_sender"
    assert decide(obs(), tri(5), state(sent_today=99)).reason == "quota_exhausted"
