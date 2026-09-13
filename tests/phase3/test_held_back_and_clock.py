"""The chat sees why the gate held something back, and the first reflex reads the clock in the person's zone."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent.client import AgentClient
from core.embeddings import FakeEmbedder
from core.repo import budget_repo, observation_repo, user_repo
from tests.conftest import USER_ID
from tests.phase1.test_worker_flow import FakeTelegram, gmail_obs
from tests.phase3.test_chat import tg_text
from workers import chat, triage


async def test_recent_observations_say_why_nothing_was_sent(conn, settings):
    held = await observation_repo.insert(conn, gmail_obs("held"))
    told = await observation_repo.insert(conn, gmail_obs("told"))
    for o, reason in ((held, "quota_exhausted"), (told, "ok")):
        await budget_repo.insert_triage(conn, o.id, urgency=4, category="calendar", reason="r", model_id="m", latency_ms=1)
        await budget_repo.set_gate_reason(conn, o.id, reason)
    await budget_repo.insert_sent(conn, USER_ID, told.id, thread_key=told.thread_key, urgency=4, tg_message_id=1)

    since = datetime.now(tz=UTC) - timedelta(hours=1)
    recent = {str(o["id"]): o for o in await chat.recent_observations(conn, USER_ID, settings, since=since)}
    assert recent[str(held.id)]["held_back"] == "quota_exhausted" and "notified_at" not in recent[str(held.id)]
    assert "notified_at" in recent[str(told.id)] and "held_back" not in recent[str(told.id)]


async def test_first_reflex_reads_the_clock_in_the_persons_zone(conn, settings):
    await user_repo.create(conn, control_source="telegram", control_thread_key="777", user_id=USER_ID,
                           display_name="Ekin", profile="Student; meeting mails are urgent.", timezone="Europe/Istanbul")
    seen: dict = {}

    def handle(payload):
        if payload["task"] == "chat_ack":
            seen.update(payload)
            return {"task": "chat_ack", "needs_work": False, "message": "Selam!"}
        raise AssertionError("a direct answer needs no second stage")

    msg = await observation_repo.insert(conn, tg_text("900", "selam", datetime.now(tz=UTC)))
    await chat.handle_message(conn, msg, settings=settings, agent=AgentClient("local", handle_fn=handle),
                              notifier=triage.Notifier(FakeTelegram(), "777", USER_ID), embedder=FakeEmbedder())
    assert datetime.fromisoformat(seen["now_iso"]).utcoffset() == timedelta(hours=3)   # Istanbul, not the server's UTC
