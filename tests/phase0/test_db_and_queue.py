from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from core import queue
from core.models import Observation
from core.repo import observation_repo
from tests.conftest import USER_ID


def _obs(key: str = "k1", **kw) -> Observation:
    base = {"user_id": USER_ID, "source": "gmail", "source_key": key, "kind": "message_in",
            "occurred_at": datetime(2026, 9, 10, 9, 0, tzinfo=UTC), "payload": {"subject": key}}
    base.update(kw)
    return Observation(**base)


async def test_dedup(conn):
    first = await observation_repo.insert(conn, _obs("dup"))
    second = await observation_repo.insert(conn, _obs("dup"))  # must not raise
    assert first is not None and second is None
    assert await observation_repo.count(conn, USER_ID, source="gmail") == 1


async def test_queue_claim_skip_locked(pool, test_db_url):
    async with pool.connection() as c:
        await observation_repo.insert(c, _obs("a"))
        await observation_repo.insert(c, _obs("b"))

    async def claim_holding_tx(url: str, hold: asyncio.Event, got: list):
        async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as c, c.transaction():
                cur = await c.execute(
                    "update observation set status='claimed', claimed_at=now(), attempts=attempts+1 "
                    "where id = (select id from observation where user_id=%s and status='new' and is_backfill=false "
                    "order by received_at for update skip locked limit 1) returning source_key",
                    (USER_ID,),
                )
                got.append((await cur.fetchone())["source_key"])
                await hold.wait()  # keep the row locked while the other claimer runs

    hold = asyncio.Event()
    got1: list = []
    t = asyncio.create_task(claim_holding_tx(test_db_url, hold, got1))
    while not got1:
        await asyncio.sleep(0.02)
    async with pool.connection() as c:
        obs2 = await queue.claim_next(c, USER_ID)
    hold.set()
    await t
    assert obs2 is not None and obs2.source_key != got1[0]
    assert {obs2.source_key, got1[0]} == {"a", "b"}


async def test_queue_recovery(conn):
    stored = await observation_repo.insert(conn, _obs("stale"))
    claimed = await queue.claim_next(conn, USER_ID)
    assert claimed.id == stored.id and claimed.attempts == 1
    await conn.execute("update observation set claimed_at = now() - interval '6 minutes' where id = %s", (stored.id,))
    assert await queue.recover_stale(conn, USER_ID) == 1
    again = await observation_repo.get(conn, stored.id)
    assert again.status == "new" and again.claimed_at is None


async def test_fail_marks_failed_after_max_attempts(conn):
    await observation_repo.insert(conn, _obs("flaky"))
    for i in range(1, queue.MAX_ATTEMPTS + 1):
        claimed = await queue.claim_next(conn, USER_ID)
        assert claimed is not None and claimed.attempts == i
        status = await queue.fail(conn, claimed.id)
    assert status == "failed"
    assert await queue.claim_next(conn, USER_ID) is None


async def test_backfill_separate_queue(conn):
    await observation_repo.insert(conn, _obs("live"))
    await observation_repo.insert(conn, _obs("old", is_backfill=True))
    live = await queue.claim_next(conn, USER_ID)
    assert live.source_key == "live"
    assert await queue.claim_next(conn, USER_ID) is None
    old = await queue.claim_next_backfill(conn, USER_ID)
    assert old.source_key == "old" and old.is_backfill
    await queue.complete(conn, live.id)
    assert (await observation_repo.get(conn, live.id)).status == "done"
    assert (await observation_repo.get(conn, old.id)).received_at - old.occurred_at > timedelta(hours=1)


async def test_claim_skips_busy_people(conn):
    other = UUID("00000000-0000-4000-8000-000000000002")
    for key in ("mine-1", "mine-2"):
        await observation_repo.insert(conn, _obs(key))
    await observation_repo.insert(conn, _obs("theirs", user_id=other))
    mine = await queue.claim_next(conn, None, exclude_users=[other])
    assert mine.user_id == USER_ID
    theirs = await queue.claim_next(conn, None, exclude_users=[USER_ID])      # I am busy: the other person is served
    assert theirs.source_key == "theirs"
    assert await queue.claim_next(conn, None, exclude_users=[USER_ID, other]) is None
    rest = await queue.claim_next(conn, None)
    assert rest.user_id == USER_ID and rest.id != mine.id


async def test_touch_keeps_a_long_claim_from_looking_stale(conn):
    stored = await observation_repo.insert(conn, _obs("long"))
    await queue.claim_next(conn, USER_ID)
    await conn.execute("update observation set claimed_at = now() - interval '6 minutes' where id = %s", (stored.id,))
    await queue.touch(conn, stored.id)
    assert await queue.recover_stale(conn, USER_ID) == 0
    assert (await observation_repo.get(conn, stored.id)).status == "claimed"
