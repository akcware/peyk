"""Queue = observation.status + claimed_at + attempts. Postgres FOR UPDATE SKIP LOCKED, no broker."""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import psycopg

from core.models import Observation

MAX_ATTEMPTS = 5
STALE_CLAIM = timedelta(minutes=5)

_RETURNING = (
    "id, user_id, source, source_key, kind, occurred_at, received_at, thread_key, payload, "
    "is_backfill, status, claimed_at, attempts"
)


async def _claim(conn: psycopg.AsyncConnection, user_id: UUID, is_backfill: bool) -> Observation | None:
    async with conn.transaction():
        cur = await conn.execute(
            f"""
            update observation set status = 'claimed', claimed_at = now(), attempts = attempts + 1
            where id = (
              select id from observation
              where user_id = %s and status = 'new' and is_backfill = %s
              order by received_at
              for update skip locked
              limit 1
            )
            returning {_RETURNING}
            """,
            (user_id, is_backfill),
        )
        row = await cur.fetchone()
    return Observation(**row) if row else None


async def claim_next(conn: psycopg.AsyncConnection, user_id: UUID) -> Observation | None:
    """Live queue: is_backfill = false."""
    return await _claim(conn, user_id, False)


async def claim_next_backfill(conn: psycopg.AsyncConnection, user_id: UUID) -> Observation | None:
    """Low-priority backfill queue: is_backfill = true (consumed only by workers/backfill.py)."""
    return await _claim(conn, user_id, True)


async def complete(conn: psycopg.AsyncConnection, obs_id: UUID) -> None:
    await conn.execute("update observation set status = 'done' where id = %s", (obs_id,))


async def fail(conn: psycopg.AsyncConnection, obs_id: UUID, *, max_attempts: int = MAX_ATTEMPTS) -> str:
    """Return to 'new' for retry, or mark 'failed' after max_attempts. Returns the resulting status."""
    cur = await conn.execute(
        """
        update observation
        set status = case when attempts >= %s then 'failed' else 'new' end, claimed_at = null
        where id = %s
        returning status
        """,
        (max_attempts, obs_id),
    )
    return (await cur.fetchone())["status"]


async def recover_stale(conn: psycopg.AsyncConnection, user_id: UUID, *, older_than: timedelta = STALE_CLAIM) -> int:
    """Crash tolerance: claimed rows older than `older_than` go back to 'new'."""
    cur = await conn.execute(
        """
        update observation set status = 'new', claimed_at = null
        where user_id = %s and status = 'claimed' and claimed_at < now() - %s
        """,
        (user_id, older_than),
    )
    return cur.rowcount
