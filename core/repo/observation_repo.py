"""All observation SQL. Dedup is here (ON CONFLICT DO NOTHING), not in adapters or workers."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from core.models import Observation

_COLS = (
    "id, user_id, source, source_key, kind, occurred_at, received_at, thread_key, payload, "
    "is_backfill, status, claimed_at, attempts"
)


def _row(r: dict[str, Any]) -> Observation:
    return Observation(**r)


async def insert(conn: psycopg.AsyncConnection, obs: Observation) -> Observation | None:
    """Insert; returns the stored row, or None if (user_id, source, source_key) already existed."""
    cur = await conn.execute(
        f"""
        insert into observation
          (user_id, source, source_key, kind, occurred_at, thread_key, payload, is_backfill)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (user_id, source, source_key) do nothing
        returning {_COLS}
        """,
        (
            obs.user_id, obs.source, obs.source_key, obs.kind, obs.occurred_at,
            obs.thread_key, Jsonb(obs.payload), obs.is_backfill,
        ),
    )
    row = await cur.fetchone()
    return _row(row) if row else None


async def get(conn: psycopg.AsyncConnection, obs_id: UUID) -> Observation | None:
    cur = await conn.execute(f"select {_COLS} from observation where id = %s", (obs_id,))
    row = await cur.fetchone()
    return _row(row) if row else None


async def count(conn: psycopg.AsyncConnection, user_id: UUID, **where: Any) -> int:
    clauses = ["user_id = %s"]
    params: list[Any] = [user_id]
    for k, v in where.items():
        clauses.append(f"{k} = %s")
        params.append(v)
    cur = await conn.execute(f"select count(*) as n from observation where {' and '.join(clauses)}", params)
    return (await cur.fetchone())["n"]


async def list_by_thread(
    conn: psycopg.AsyncConnection, user_id: UUID, thread_key: str, *, limit: int = 20,
    kinds: tuple[str, ...] = ("message_in", "message_out"),
) -> list[Observation]:
    cur = await conn.execute(
        f"""
        select {_COLS} from observation
        where user_id = %s and thread_key = %s and kind = any(%s)
        order by occurred_at desc limit %s
        """,
        (user_id, thread_key, list(kinds), limit),
    )
    return [_row(r) for r in await cur.fetchall()]


async def list_since(
    conn: psycopg.AsyncConnection, user_id: UUID, since: datetime, *, limit: int = 200
) -> list[Observation]:
    cur = await conn.execute(
        f"select {_COLS} from observation where user_id = %s and occurred_at >= %s "
        "order by occurred_at desc limit %s",
        (user_id, since, limit),
    )
    return [_row(r) for r in await cur.fetchall()]


async def count_from_sender(conn: psycopg.AsyncConnection, user_id: UUID, sender_email: str) -> int:
    """How many earlier observations carry this sender address in payload.from (case-insensitive)."""
    cur = await conn.execute(
        "select count(*) as n from observation where user_id = %s and payload->>'from' ilike %s",
        (user_id, f"%{sender_email}%"),
    )
    return (await cur.fetchone())["n"]
