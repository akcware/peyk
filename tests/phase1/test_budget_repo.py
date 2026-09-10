from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent.schemas import TriageResult
from core.models import Observation
from core.repo import budget_repo, observation_repo
from tests.conftest import USER_ID
from workers import gate_state
from workers.gate import decide


def _obs(key: str, thread: str = "t1") -> Observation:
    return Observation(user_id=USER_ID, source="gmail", source_key=key, kind="message_in",
                       occurred_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC), thread_key=thread,
                       payload={"from": "Mara <Mara@Example-Client.test>", "subject": key})


async def test_gate_state_from_db_and_quiet_hours_wrap(conn):
    now = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
    stored = await observation_repo.insert(conn, _obs("a"))
    st = await gate_state.load(conn, stored, now)
    assert st.settings.daily_quota == 5 and st.settings.quiet_hours is None and st.sender == "mara@example-client.test"
    assert decide(stored, TriageResult(urgency=3, category="person", reason="r"), st).notify

    await budget_repo.set_quiet_hours(conn, USER_ID, 23, 8)
    st = await gate_state.load(conn, stored, now)
    assert st.settings.quiet_hours == (23, 8)
    night = now.replace(hour=23, minute=30)
    assert decide(stored, TriageResult(urgency=3, category="person", reason="r"), await gate_state.load(conn, stored, night)).reason == "quiet_hours"


async def test_sent_today_cooldown_and_mutes(conn):
    now = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
    a = await observation_repo.insert(conn, _obs("a"))
    b = await observation_repo.insert(conn, _obs("b", thread="t2"))
    await budget_repo.insert_triage(conn, a.id, urgency=4, category="person", reason="r", model_id="m", latency_ms=12)
    assert (await budget_repo.get_triage(conn, a.id))["urgency"] == 4
    sid = await budget_repo.insert_sent(conn, USER_ID, a.id, thread_key="t1", urgency=4, tg_message_id=77)
    await conn.execute("update sent_notification set sent_at = %s where id = %s", (now - timedelta(minutes=10), sid))

    st = await gate_state.load(conn, a, now)
    assert st.sent_today == 1 and st.last_sent_in_thread_at == now - timedelta(minutes=10)
    assert decide(a, TriageResult(urgency=4, category="person", reason="r"), st).reason == "thread_cooldown"
    st_b = await gate_state.load(conn, b, now)
    assert st_b.last_sent_in_thread_at is None
    assert decide(b, TriageResult(urgency=4, category="person", reason="r"), st_b).notify

    assert await budget_repo.set_feedback(conn, USER_ID, sid, "noise")
    assert (await budget_repo.find_sent_by_tg_message(conn, USER_ID, 77))["user_feedback"] == "noise"

    await budget_repo.add_mute(conn, USER_ID, "thread", "t2")
    await budget_repo.add_mute(conn, USER_ID, "sender", "old@example.test", until=now - timedelta(days=1))
    st_b = await gate_state.load(conn, b, now)
    assert [m.value for m in st_b.mutes] == ["t2"]  # expired mute filtered in SQL
    assert decide(b, TriageResult(urgency=5, category="person", reason="r"), st_b).reason == "muted_sender"
