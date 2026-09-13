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
          (user_id, source, source_key, kind, occurred_at, thread_key, payload, is_backfill, status)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (user_id, source, source_key) do nothing
        returning {_COLS}
        """,
        (
            obs.user_id, obs.source, obs.source_key, obs.kind, obs.occurred_at,
            obs.thread_key, Jsonb(obs.payload), obs.is_backfill,
            "done" if obs.kind == "message_out" else obs.status,   # our own outbound records are not work
        ),
    )
    row = await cur.fetchone()
    return _row(row) if row else None


async def get(conn: psycopg.AsyncConnection, obs_id: UUID) -> Observation | None:
    cur = await conn.execute(f"select {_COLS} from observation where id = %s", (obs_id,))
    row = await cur.fetchone()
    return _row(row) if row else None


async def update_payload(conn: psycopg.AsyncConnection, obs_id: UUID, payload: dict[str, Any]) -> None:
    """What a review learned (e.g. what a calendar change was) is kept with the observation for later readers."""
    await conn.execute("update observation set payload = %s where id = %s", (Jsonb(payload), obs_id))


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


async def claim_followups(
    conn: psycopg.AsyncConnection, obs: Observation, *, kinds: tuple[str, ...] = ("message_in",)
) -> list[Observation]:
    """Take (mark done) the newer, still-unprocessed messages in the same thread from the same user so they
    can be merged into the current turn instead of being answered one by one."""
    async with conn.transaction():
        cur = await conn.execute(
            f"""
            update observation set status = 'done', claimed_at = now(), attempts = attempts + 1
            where id in (
              select id from observation
              where user_id = %s and source = %s and thread_key = %s and kind = any(%s)
                and status = 'new' and received_at > %s
              order by received_at
              for update skip locked
            )
            returning {_COLS}
            """,
            (obs.user_id, obs.source, obs.thread_key, list(kinds), obs.received_at),
        )
        rows = await cur.fetchall()
    return sorted((_row(r) for r in rows), key=lambda o: o.received_at)
