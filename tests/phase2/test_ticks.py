from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adapters.composio.adapter import ComposioAdapter
from agent.schemas import TriageResult
from core.adapter import AdapterRegistry
from core.models import Observation
from core.repo import budget_repo, cursor_repo, job_repo, observation_repo
from tests.conftest import USER_ID
from tests.phase1.test_worker_flow import FakeTelegram, fake_agent, gmail_obs
from workers import scheduler, ticks, triage

FIX = Path(__file__).resolve().parents[1] / "fixtures"


async def _tick(conn, kind: str, payload: dict | None = None) -> Observation:
    now = datetime.now(tz=UTC)
    job = await job_repo.create(conn, USER_ID, run_at=now, kind=kind, payload=payload or {}, created_by="system")
    return await observation_repo.insert(conn, scheduler.tick_observation(job))


async def test_morning_brief_content(conn, settings):
    now = datetime.now(tz=UTC)
    for key, urgency in (("a", 5), ("b", 3), ("c", 2), ("d", 1), ("e", 4)):
        o = await observation_repo.insert(conn, gmail_obs(key))
        await conn.execute("update observation set occurred_at = %s where id = %s", (now - timedelta(hours=2), o.id))
        await budget_repo.insert_triage(conn, o.id, urgency=urgency, category="person", reason=f"reason-{key}", model_id="m", latency_ms=1)
    old = await observation_repo.insert(conn, gmail_obs("old"))
    await conn.execute("update observation set occurred_at = %s where id = %s", (now - timedelta(hours=30), old.id))
    await budget_repo.insert_triage(conn, old.id, urgency=5, category="person", reason="reason-old", model_id="m", latency_ms=1)

    items = await ticks.brief_items(conn, USER_ID, now - timedelta(hours=24))
    text = ticks.render_brief(items, now)
    assert "subject a" in text and "subject b" in text and "subject e" in text
    assert "subject c" not in text and "subject d" not in text and "subject old" not in text
    assert text.index("subject a") < text.index("subject e") < text.index("subject b")   # urgency desc
    assert "Good morning" in text and "item(s)" not in text
    assert "Nothing important" in ticks.render_brief([], now)
    assert ticks.render_brief([], now, "tr").startswith("Günaydın")

    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)
    tick = await _tick(conn, "morning_brief")
    await triage.handle(tick, settings=settings, agent=fake_agent(1), notifier=notifier)
    assert len(tg.sent) == 1 and "Good morning" in tg.sent[0]["text"]


async def test_followup_and_remind_command(conn, settings):
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)
    cmd = await observation_repo.insert(conn, Observation(user_id=USER_ID, source="telegram", source_key="c1", kind="message_in",
                                                          occurred_at=datetime.now(tz=UTC), thread_key="777",
                                                          payload={"update_id": 1, "message": {"text": "/remind 2m test note", "chat": {"id": 777}}}))
    await triage.handle(cmd, settings=settings, agent=fake_agent(1), notifier=notifier)
    jobs = await job_repo.pending_of_kind(conn, USER_ID, "followup")
    assert len(jobs) == 1 and jobs[0]["payload"] == {"note": "test note"} and jobs[0]["created_by"] == "user"
    assert "I'll remind you" in tg.sent[-1]["text"]

    fired = await scheduler.fire_due(conn, USER_ID, jobs[0]["run_at"] + timedelta(seconds=1), settings.TIMEZONE)
    assert fired == 1
    tick = next(o for o in await observation_repo.list_since(conn, USER_ID, datetime.now(tz=UTC) - timedelta(days=1)) if o.kind == "tick")
    await triage.handle(tick, settings=settings, agent=fake_agent(1), notifier=notifier)
    assert tg.sent[-1]["text"] == "⏰ Reminder: test note"


async def test_recheck_thread_retriages(conn, settings):
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)
    o = await observation_repo.insert(conn, gmail_obs("rt"))
    tick = await _tick(conn, "recheck_thread", {"thread_key": o.thread_key})
    await triage.handle(tick, settings=settings, agent=fake_agent(4), notifier=notifier)
    assert (await budget_repo.get_triage(conn, o.id))["urgency"] == 4
    assert len(tg.sent) == 1


def _pages():
    return {None: json.loads((FIX / "gmail_messages" / "fetch_emails_page1.json").read_text()),
            "page-2-token": json.loads((FIX / "gmail_messages" / "fetch_emails_page2.json").read_text())}


async def test_reconcile_fills_gap(conn, settings):
    """3 of 5 messages arrived via trigger; GMAIL_FETCH_EMAILS returns all 5 (paged) -> 2 new rows, live, untouched 3."""
    pages = _pages()
    extra = {"messageId": "19b1170000000004", "threadId": "t4", "sender": "X <x@example.test>", "subject": "four",
             "messageTimestamp": "2026-09-09T12:00:00Z", "preview": {"body": "4"}}
    extra2 = {**extra, "messageId": "19b1170000000005", "subject": "five", "messageTimestamp": "2026-09-09T12:30:00Z"}
    pages["page-2-token"]["data"]["messages"] += [extra, extra2]
    adapter = ComposioAdapter(settings=settings, execute=lambda slug, args, *, user_id: pages[args.get("page_token")])
    registry = AdapterRegistry({"composio": adapter})

    seen = []
    for key in ("19b1170000000001", "19b1170000000002", "19b1170000000003"):
        o = await observation_repo.insert(conn, Observation(user_id=USER_ID, source="gmail", source_key=key, kind="message_in",
                                                            occurred_at=datetime(2026, 9, 9, 8, 0, tzinfo=UTC), payload={"subject": "via trigger"}))
        await conn.execute("update observation set status='done' where id = %s", (o.id,))
        seen.append(o)
    await cursor_repo.set(conn, USER_ID, "composio", datetime(2026, 9, 9, 11, 45, tzinfo=UTC).isoformat())

    tg = FakeTelegram()
    ctx = ticks.TickContext(settings=settings, registry=registry, notifier=triage.Notifier(tg, "777", USER_ID))
    tick = await _tick(conn, "reconcile")
    await ticks.handle_tick(conn, tick, ctx)

    assert await observation_repo.count(conn, USER_ID, source="gmail") == 5
    for o in seen:   # untouched: still done, same payload
        again = await observation_repo.get(conn, o.id)
        assert again.status == "done" and again.payload == {"subject": "via trigger"}
    new = [await observation_repo.get(conn, r["id"]) for r in await (await conn.execute(
        "select id from observation where source='gmail' and source_key in ('19b1170000000004','19b1170000000005')")).fetchall()]
    assert len(new) == 2 and all(o.status == "new" and o.is_backfill is False for o in new)
    assert await cursor_repo.get(conn, USER_ID, "composio") == datetime(2026, 9, 9, 12, 30, tzinfo=UTC).isoformat()


async def test_reconcile_no_duplicate_notification(conn, settings):
    pages = _pages()
    adapter = ComposioAdapter(settings=settings, execute=lambda slug, args, *, user_id: pages[args.get("page_token")])
    registry = AdapterRegistry({"composio": adapter})
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)

    # message 2 arrived via trigger and was already notified
    o = await observation_repo.insert(conn, Observation(user_id=USER_ID, source="gmail", source_key="19b1170000000002", kind="message_in",
                                                        occurred_at=datetime(2026, 9, 9, 8, 30, tzinfo=UTC), thread_key="19b1170000000002",
                                                        payload={"from": "Dr. Okafor <okafor@example-clinic.test>", "subject": "Appointment confirmation"}))
    await triage.handle(o, settings=settings, agent=fake_agent(4), notifier=notifier, now=o.occurred_at + timedelta(minutes=1))
    assert len(tg.sent) == 1

    ctx = ticks.TickContext(settings=settings, registry=registry, notifier=notifier)
    await ticks.handle_tick(conn, await _tick(conn, "reconcile"), ctx)
    # replay the live queue for whatever reconcile added
    from core import queue
    while (nxt := await queue.claim_next(conn, USER_ID)) is not None:
        await triage.handle(nxt, settings=settings, agent=fake_agent(4), notifier=notifier, now=nxt.occurred_at + timedelta(minutes=1))
        await queue.complete(conn, nxt.id)
    cur = await conn.execute("select count(*) as n from sent_notification where observation_id = %s", (o.id,))
    assert (await cur.fetchone())["n"] == 1
    assert await observation_repo.count(conn, USER_ID, source="gmail", source_key="19b1170000000002") == 1


def test_tick_unknown_kind_is_noop():
    assert "morning_brief" in ticks.HANDLERS and "reconcile" in ticks.HANDLERS
    assert TriageResult(urgency=1, category="other", reason="x").urgency == 1


async def test_reconcile_bootstrap_sets_cursor_without_replay(conn, settings):
    pages = _pages()
    calls = []
    def execute(slug, args, *, user_id):
        calls.append(slug); return pages[args.get("page_token")]
    adapter = ComposioAdapter(settings=settings, execute=execute)
    ctx = ticks.TickContext(settings=settings, registry=AdapterRegistry({"composio": adapter}), notifier=triage.Notifier(FakeTelegram(), "777", USER_ID))
    await ticks.handle_tick(conn, await _tick(conn, "reconcile"), ctx)
    assert calls == []                                                       # no fetch on first run
    assert await observation_repo.count(conn, USER_ID, source="gmail") == 0
    assert await cursor_repo.get(conn, USER_ID, "composio") is not None
    await ticks.handle_tick(conn, await _tick(conn, "reconcile"), ctx)       # second run replays since cursor - lookback
    assert calls and await observation_repo.count(conn, USER_ID, source="gmail") == 3


async def test_morning_brief_is_agent_written_when_agent_available(conn, settings):
    from agent.client import AgentClient
    from core.embeddings import FakeEmbedder
    from core.repo import user_repo

    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "777", language="tr")
    o = await observation_repo.insert(conn, Observation(**{**gmail_obs("mb").model_dump(), "user_id": user["id"], "occurred_at": datetime.now(tz=UTC)}))
    await budget_repo.insert_triage(conn, o.id, urgency=4, category="person", reason="r", model_id="m", latency_ms=1, summary="Mara faturayı bekliyor")
    seen = []
    def voice(payload):
        if payload["task"] == "chat":
            seen.append(payload["message"]); return {"task": "chat", "reply": "Günaydın! Bugün tek önemli şey: Mara faturayı bekliyor.", "intents": []}
        return {"task": "chat_ack", "needs_work": True, "message": ""}
    tg = FakeTelegram()
    ctx = ticks.TickContext(settings=settings, registry=None, notifier=triage.Notifier(tg, "777", user["id"]),
                            agent=AgentClient("local", handle_fn=voice), embedder=FakeEmbedder())
    job = await job_repo.create(conn, user["id"], run_at=datetime.now(tz=UTC), kind="morning_brief", created_by="system")
    await ticks.handle_tick(conn, await observation_repo.insert(conn, scheduler.tick_observation(job)), ctx)
    assert seen and "morning brief" in seen[0] and "Mara faturayı bekliyor" in seen[0]
    assert tg.sent[-1]["text"].startswith("Günaydın!")


async def test_brief_skips_own_mail_and_answered_threads_and_dates_items(conn, settings):
    """The brief must not list what the person wrote themselves, nor a thread they already replied to; every item
    it does list says when it arrived (in the person's timezone) and whether they were already told."""
    from zoneinfo import ZoneInfo

    now = datetime.now(tz=ZoneInfo("Europe/Berlin")).replace(hour=9, minute=3, second=0, microsecond=0)
    yesterday_1730 = (now - timedelta(days=1)).replace(hour=17, minute=30)

    async def stored(key, occurred_at, *, kind="message_in", thread=None, labels=None, urgency=4, sender="Mara <mara@example-client.test>"):
        o = gmail_obs(key, sender=sender)
        o.kind, o.occurred_at = kind, occurred_at
        o.thread_key = thread or o.thread_key
        if labels:
            o.payload["label_ids"] = labels
        o = await observation_repo.insert(conn, o)
        await budget_repo.insert_triage(conn, o.id, urgency=urgency, category="person", reason=f"reason-{key}", model_id="m", latency_ms=1, summary=f"summary {key}")
        return o

    fly = await stored("fly", yesterday_1730)                                                   # old one-time link
    await stored("mine", now - timedelta(hours=3), kind="message_out", labels=["SENT"], sender="Me <me@example.test>")
    await stored("baris", now - timedelta(hours=20), thread="th-wg")                             # Barış wrote ...
    await stored("reply", now - timedelta(hours=19), thread="th-wg", kind="message_out", labels=["SENT"], sender="Me <me@example.test>")  # ... and the person answered
    await stored("legacy", now - timedelta(hours=18), thread="th-old")                          # older rows: SENT copy still kind message_in
    await stored("legacy-reply", now - timedelta(hours=17), thread="th-old", labels=["SENT"], sender="Me <me@example.test>")
    fresh = await stored("fresh", now - timedelta(minutes=50))
    await budget_repo.insert_sent(conn, USER_ID, fresh.id, thread_key=fresh.thread_key, urgency=4, tg_message_id=1)

    items = await ticks.brief_items(conn, USER_ID, now - timedelta(hours=24))
    keys = [i["payload"]["subject"] for i in items]
    assert keys == ["subject fresh", "subject fly"]          # own mail, answered threads (new and legacy shape) are gone
    assert items[0]["notified_at"] is not None and items[1]["notified_at"] is None

    text = ticks.brief_event_text(items, now)
    assert "yesterday 17:30 (15h ago)" in text and "today 08:13 (0h ago)" in text
    assert "; you told them at " in text and "expire within minutes" in text and "first name" in text
    assert ticks.when_label(fly.occurred_at - timedelta(days=3), now).endswith("(87h ago)".replace("87h", "3d"))
