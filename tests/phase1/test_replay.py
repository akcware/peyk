"""60 labeled mails -> fixed TriageResults -> decide(). No LLM. Spread over 3 days (20/day) so the
daily quota is exercised; every label-5 mail must get through, and sends per day <= quota."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.schemas import TriageResult
from core.models import Observation
from tests.conftest import USER_ID
from workers.gate import BudgetSettings, GateState, decide

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "labeled_triage.jsonl"


def load():
    return [json.loads(line) for line in FIX.read_text().splitlines() if line.strip()]


def test_fixture_shape():
    rows = load()
    assert len(rows) == 60
    assert all(1 <= r["label_urgency"] <= 5 for r in rows)
    assert len({r["source_key"] for r in rows}) == 60


def test_gate_replay_60_to_few():
    rows = load()
    settings = BudgetSettings(daily_quota=5, thread_cooldown_minutes=240, quiet_hours=None, bypass_urgency=5)
    sent_per_day: dict[int, int] = defaultdict(int)
    last_in_thread: dict[str, datetime] = {}
    reasons = Counter()
    missed_fives = []
    for i, r in enumerate(rows):
        day = i // 20
        now = datetime(2026, 9, 8, 9, 0, tzinfo=UTC) + timedelta(days=day, minutes=20 * (i % 20))
        obs = Observation(user_id=USER_ID, source="gmail", source_key=r["source_key"], kind="message_in",
                          occurred_at=now, thread_key=r["source_key"],
                          payload={"from": r["from"], "subject": r["subject"], "snippet": r["snippet"]})
        tri = TriageResult(urgency=r["label_urgency"], category=r["label_category"], reason="label")
        st = GateState(sent_today=sent_per_day[day], last_sent_in_thread_at=last_in_thread.get(obs.thread_key),
                       mutes=[], settings=settings, now=now)
        d = decide(obs, tri, st)
        reasons[d.reason] += 1
        if d.notify:
            sent_per_day[day] += 1
            last_in_thread[obs.thread_key] = now
        elif r["label_urgency"] == 5:
            missed_fives.append(r["subject"])
    assert missed_fives == [], f"urgency-5 mails blocked: {missed_fives}"
    assert all(n <= settings.daily_quota for n in sent_per_day.values()), dict(sent_per_day)
    total = sum(sent_per_day.values())
    assert total <= 3 * settings.daily_quota
    assert total < 60 / 3  # "60 a day down to a handful"
