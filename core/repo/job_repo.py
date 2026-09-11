"""scheduled_job SQL. Claiming uses FOR UPDATE SKIP LOCKED so two schedulers never double-fire."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

_COLS = "id, user_id, run_at, kind, payload, recurrence, status, created_by, claimed_at, created_at"


async def create(
    conn: psycopg.AsyncConnection, user_id: UUID, *, run_at: datetime, kind: str, payload: dict[str, Any] | None = None,
    recurrence: str | None = None, created_by: str,
) -> dict:
    cur = await conn.execute(
        f"""
        insert into scheduled_job (user_id, run_at, kind, payload, recurrence, created_by)
        values (%s, %s, %s, %s, %s, %s) returning {_COLS}
        """,
        (user_id, run_at, kind, Jsonb(payload or {}), recurrence, created_by),
    )
    return await cur.fetchone()


async def claim_due(conn: psycopg.AsyncConnection, user_id: UUID | None, now: datetime, *, limit: int = 10) -> list[dict]:
    """user_id=None claims due jobs of all users."""
    user_clause = "user_id = %s and " if user_id is not None else ""
    params: tuple = (user_id, now, limit) if user_id is not None else (now, limit)
    async with conn.transaction():
        cur = await conn.execute(
            f"""
            update scheduled_job set status = 'claimed', claimed_at = now()
            where id in (
              select id from scheduled_job
              where {user_clause}status = 'pending' and run_at <= %s
              order by run_at
              for update skip locked
              limit %s
            )
            returning {_COLS}
            """,
            params,
        )
        return await cur.fetchall()


async def mark(conn: psycopg.AsyncConnection, job_id: UUID, status: str) -> None:
    await conn.execute("update scheduled_job set status = %s where id = %s", (status, job_id))


async def pending_of_kind(conn: psycopg.AsyncConnection, user_id: UUID, kind: str) -> list[dict]:
    cur = await conn.execute(
        f"select {_COLS} from scheduled_job where user_id = %s and kind = %s and status = 'pending' order by run_at",
        (user_id, kind),
    )
    return await cur.fetchall()


async def cancel_kind(conn: psycopg.AsyncConnection, user_id: UUID, kind: str) -> int:
    cur = await conn.execute(
        "update scheduled_job set status = 'cancelled' where user_id = %s and kind = %s and status = 'pending'",
        (user_id, kind),
    )
    return cur.rowcount


async def get(conn: psycopg.AsyncConnection, job_id: UUID) -> dict | None:
    cur = await conn.execute(f"select {_COLS} from scheduled_job where id = %s", (job_id,))
    return await cur.fetchone()


async def cancel_matching(conn: psycopg.AsyncConnection, user_id: UUID, kind: str, payload_key: str, value: str) -> int:
    cur = await conn.execute(
        "update scheduled_job set status = 'cancelled' where user_id = %s and kind = %s and status = 'pending' and payload->>%s = %s",
        (user_id, kind, payload_key, value),
    )
    return cur.rowcount
