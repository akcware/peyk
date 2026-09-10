from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from core.recurrence import next_run, parse_duration
from core.repo import job_repo, observation_repo
from tests.conftest import USER_ID
from workers import scheduler
from workers.commands import parse_remind


async def test_job_fires_once(conn, settings):
    now = datetime.now(tz=UTC)
    job = await job_repo.create(conn, USER_ID, run_at=now + timedelta(seconds=2), kind="followup",
                                payload={"note": "hi"}, created_by="user")
    assert await scheduler.fire_due(conn, USER_ID, now, settings.TIMEZONE) == 0      # not due yet
    assert await scheduler.fire_due(conn, USER_ID, now + timedelta(seconds=5), settings.TIMEZONE) == 1
    assert await scheduler.fire_due(conn, USER_ID, now + timedelta(seconds=60), settings.TIMEZONE) == 0  # no second tick
    ticks = await observation_repo.list_since(conn, USER_ID, now - timedelta(days=1))
    ticks = [t for t in ticks if t.kind == "tick"]
    assert len(ticks) == 1
    assert ticks[0].source == "system" and ticks[0].source_key == str(job["id"]) and ticks[0].status == "new"
    assert ticks[0].payload["job"]["kind"] == "followup" and ticks[0].payload["job"]["payload"] == {"note": "hi"}
    assert (await job_repo.get(conn, job["id"]))["status"] == "done"


async def test_recurrence_reschedules(conn, settings):
    tz = "Europe/Berlin"
    now = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)   # 08:00 Berlin
    job = await job_repo.create(conn, USER_ID, run_at=now, kind="morning_brief", recurrence="daily@08:00", created_by="system")
    assert await scheduler.fire_due(conn, USER_ID, now, tz) == 1
    pending = await job_repo.pending_of_kind(conn, USER_ID, "morning_brief")
    assert len(pending) == 1 and pending[0]["id"] != job["id"]
    assert pending[0]["run_at"] == datetime(2026, 9, 11, 6, 0, tzinfo=UTC)   # next day 08:00 Berlin (CEST)
    assert pending[0]["recurrence"] == "daily@08:00" and pending[0]["created_by"] == "system"


def test_recurrence_math():
    base = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    assert next_run("every:10m", base) == base + timedelta(minutes=10)
    assert next_run("every:2h", base) == base + timedelta(hours=2)
    assert next_run("daily@08:00", base, "Europe/Berlin") == datetime(2026, 9, 11, 6, 0, tzinfo=UTC)
    assert next_run("daily@23:30", base, "Europe/Berlin") == datetime(2026, 9, 10, 21, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        next_run("cron:* * * * *", base)
    assert parse_duration("30m") == timedelta(minutes=30) and parse_duration("2h") == timedelta(hours=2)


async def test_two_schedulers_no_double_fire(pool, test_db_url, settings):
    now = datetime.now(tz=UTC)
    async with pool.connection() as c:
        for i in range(6):
            await job_repo.create(c, USER_ID, run_at=now - timedelta(seconds=i), kind="followup", payload={"n": i}, created_by="user")

    async def one_scheduler():
        async with await psycopg.AsyncConnection.connect(test_db_url, autocommit=True, row_factory=dict_row) as c:
            total = 0
            for _ in range(3):
                total += await scheduler.fire_due(c, USER_ID, now + timedelta(seconds=1), settings.TIMEZONE)
                await asyncio.sleep(0.01)
            return total

    fired = await asyncio.gather(one_scheduler(), one_scheduler())
    assert sum(fired) == 6
    async with pool.connection() as c:
        assert await observation_repo.count(c, USER_ID, kind="tick") == 6
        cur = await c.execute("select status, count(*) as n from scheduled_job group by 1")
        assert {r["status"]: r["n"] for r in await cur.fetchall()} == {"done": 6}


async def test_ensure_default_jobs_idempotent(conn, settings):
    await scheduler.ensure_default_jobs(conn, settings)
    await scheduler.ensure_default_jobs(conn, settings)
    assert len(await job_repo.pending_of_kind(conn, USER_ID, "morning_brief")) == 1
    assert len(await job_repo.pending_of_kind(conn, USER_ID, "reconcile")) == 1
    rec = (await job_repo.pending_of_kind(conn, USER_ID, "reconcile"))[0]
    assert rec["recurrence"] == "every:10m" and rec["created_by"] == "system"


def test_remind_command_parses():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)   # 14:00 Berlin
    tz = "Europe/Berlin"
    r = parse_remind("/remind 1h check the deploy", now, tz)
    assert r.run_at == now + timedelta(hours=1) and r.note == "check the deploy"
    r = parse_remind("/remind 30m  call Mara", now, tz)
    assert r.run_at == now + timedelta(minutes=30) and r.note == "call Mara"
    r = parse_remind("/remind 09:30 standup notes", now, tz)              # 09:30 already passed -> tomorrow
    assert r.run_at == datetime(2026, 9, 11, 7, 30, tzinfo=UTC) and r.note == "standup notes"
    r = parse_remind("/remind 15:00 invoice", now, tz)                     # later today
    assert r.run_at == datetime(2026, 9, 10, 13, 0, tzinfo=UTC)
    r = parse_remind("/remind tomorrow water the plants", now, tz)         # default 09:00
    assert r.run_at == datetime(2026, 9, 11, 7, 0, tzinfo=UTC)
    r = parse_remind("/remind tomorrow 18:15 gym", now, tz)
    assert r.run_at == datetime(2026, 9, 11, 16, 15, tzinfo=UTC) and r.note == "gym"
    r = parse_remind("/remind@proactiveagent_bot 2h x", now, tz)
    assert r.note == "x"
    for bad in ("/remind", "/remind soon x", "/remind 1h"):
        with pytest.raises(ValueError):
            parse_remind(bad, now, tz)
