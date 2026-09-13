"""Queue = observation.status + claimed_at + attempts. Postgres FOR UPDATE SKIP LOCKED, no broker."""
from __future__ import annotations

from collections.abc import Collection
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


async def _claim(conn: psycopg.AsyncConnection, user_id: UUID | None, is_backfill: bool,
                 exclude_users: Collection[UUID] = ()) -> Observation | None:
    user_clause = "user_id = %s and " if user_id is not None else ""
    busy_clause = "user_id <> all(%s) and " if exclude_users else ""
    params: list = [user_id] if user_id is not None else []
    if exclude_users:
        params.append(list(exclude_users))
    params.append(is_backfill)
    async with conn.transaction():
        cur = await conn.execute(
            f"""
            update observation set status = 'claimed', claimed_at = now(), attempts = attempts + 1
            where id = (
              select id from observation
              where {user_clause}{busy_clause}status = 'new' and is_backfill = %s
              order by received_at
              for update skip locked
              limit 1
            )
            returning {_RETURNING}
            """,
            params,
        )
        row = await cur.fetchone()
    return Observation(**row) if row else None


async def claim_next(conn: psycopg.AsyncConnection, user_id: UUID | None = None, *,
                     exclude_users: Collection[UUID] = ()) -> Observation | None:
    """Live queue: is_backfill = false. user_id=None claims across all users; `exclude_users` are people whose turn
    is already in progress (their next observation waits for it)."""
    return await _claim(conn, user_id, False, exclude_users)


async def claim_next_backfill(conn: psycopg.AsyncConnection, user_id: UUID) -> Observation | None:
    """Low-priority backfill queue: is_backfill = true (consumed only by workers/backfill.py)."""
    return await _claim(conn, user_id, True)


async def complete(conn: psycopg.AsyncConnection, obs_id: UUID) -> None:
    await conn.execute("update observation set status = 'done' where id = %s", (obs_id,))


async def touch(conn: psycopg.AsyncConnection, obs_id: UUID) -> None:
    """Heartbeat of a long turn: a claim that is still being worked on must not look stale to recover_stale()."""
    await conn.execute("update observation set claimed_at = now() where id = %s and status = 'claimed'", (obs_id,))


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


async def recover_stale(conn: psycopg.AsyncConnection, user_id: UUID | None = None, *, older_than: timedelta = STALE_CLAIM) -> int:
    """Crash tolerance: claimed rows older than `older_than` go back to 'new'. user_id=None: all users."""
    if user_id is None:
        cur = await conn.execute(
            "update observation set status = 'new', claimed_at = null where status = 'claimed' and claimed_at < now() - %s",
            (older_than,))
    else:
        cur = await conn.execute(
            "update observation set status = 'new', claimed_at = null where user_id = %s and status = 'claimed' and claimed_at < now() - %s",
            (user_id, older_than))
    return cur.rowcount
