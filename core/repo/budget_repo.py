"""triage / sent_notification / mute_rule / budget_settings SQL. Builds GateState inputs for workers/gate.py."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import psycopg


async def insert_triage(
    conn: psycopg.AsyncConnection, observation_id: UUID, *, urgency: int, category: str, reason: str,
    model_id: str, latency_ms: int | None, summary: str = "",
) -> None:
    await conn.execute(
        """
        insert into triage (observation_id, urgency, category, reason, summary, model_id, latency_ms)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (observation_id) do update
          set urgency = excluded.urgency, category = excluded.category, reason = excluded.reason,
              summary = excluded.summary, model_id = excluded.model_id, latency_ms = excluded.latency_ms, created_at = now()
        """,
        (observation_id, urgency, category, reason, summary, model_id, latency_ms),
    )


async def get_triage(conn: psycopg.AsyncConnection, observation_id: UUID) -> dict | None:
    cur = await conn.execute("select * from triage where observation_id = %s", (observation_id,))
    return await cur.fetchone()


async def insert_sent(
    conn: psycopg.AsyncConnection, user_id: UUID, observation_id: UUID | None, *, thread_key: str | None,
    urgency: int | None, tg_message_id: int | None,
) -> UUID:
    cur = await conn.execute(
        """
        insert into sent_notification (user_id, observation_id, thread_key, urgency, tg_message_id)
        values (%s, %s, %s, %s, %s) returning id
        """,
        (user_id, observation_id, thread_key, urgency, tg_message_id),
    )
    return (await cur.fetchone())["id"]


async def sent_today(conn: psycopg.AsyncConnection, user_id: UUID, now: datetime) -> int:
    """Notifications sent in the last 24h (rolling window; simpler and stricter than calendar day)."""
    cur = await conn.execute(
        "select count(*) as n from sent_notification where user_id = %s and sent_at > %s - interval '24 hours'",
        (user_id, now),
    )
    return (await cur.fetchone())["n"]


async def last_sent_in_thread(conn: psycopg.AsyncConnection, user_id: UUID, thread_key: str | None) -> datetime | None:
    if not thread_key:
        return None
    cur = await conn.execute(
        "select max(sent_at) as t from sent_notification where user_id = %s and thread_key = %s",
        (user_id, thread_key),
    )
    return (await cur.fetchone())["t"]


async def set_feedback(conn: psycopg.AsyncConnection, user_id: UUID, sent_id: UUID, feedback: str) -> bool:
    cur = await conn.execute(
        "update sent_notification set user_feedback = %s where id = %s and user_id = %s",
        (feedback, sent_id, user_id),
    )
    return cur.rowcount == 1


async def find_sent_by_tg_message(conn: psycopg.AsyncConnection, user_id: UUID, tg_message_id: int) -> dict | None:
    cur = await conn.execute(
        "select * from sent_notification where user_id = %s and tg_message_id = %s order by sent_at desc limit 1",
        (user_id, tg_message_id),
    )
    return await cur.fetchone()


async def add_mute(
    conn: psycopg.AsyncConnection, user_id: UUID, kind: str, value: str, until: datetime | None = None
) -> None:
    await conn.execute(
        """
        insert into mute_rule (user_id, kind, value, until) values (%s, %s, %s, %s)
        on conflict (user_id, kind, value) do update set until = excluded.until
        """,
        (user_id, kind, value, until),
    )


async def list_mutes(conn: psycopg.AsyncConnection, user_id: UUID, now: datetime) -> list[dict]:
    cur = await conn.execute(
        "select kind, value, until from mute_rule where user_id = %s and (until is null or until > %s)",
        (user_id, now),
    )
    return await cur.fetchall()


async def get_budget_settings(conn: psycopg.AsyncConnection, user_id: UUID) -> dict:
    """Returns a dict with daily_quota, thread_cooldown_minutes, quiet_hours (tuple|None), bypass_urgency.
    Inserts defaults on first call so the row always exists."""
    cur = await conn.execute(
        """
        insert into budget_settings (user_id) values (%s) on conflict (user_id) do nothing;
        """,
        (user_id,),
    )
    cur = await conn.execute(
        "select daily_quota, thread_cooldown_minutes, lower(quiet_hours) as qh_start, upper(quiet_hours) as qh_end, "
        "bypass_urgency from budget_settings where user_id = %s",
        (user_id,),
    )
    row = await cur.fetchone()
    qh = (row["qh_start"] % 24, row["qh_end"] % 24) if row["qh_start"] is not None and row["qh_end"] is not None else None
    return {
        "daily_quota": row["daily_quota"],
        "thread_cooldown_minutes": row["thread_cooldown_minutes"],
        "quiet_hours": qh,
        "bypass_urgency": row["bypass_urgency"],
    }


async def set_quiet_hours(conn: psycopg.AsyncConnection, user_id: UUID, start: int | None, end: int | None) -> None:
    """Stores [start, end) hours; a wrapped window like 23→8 is stored as [23, 32) and read back mod 24."""
    if start is None or end is None:
        await conn.execute("update budget_settings set quiet_hours = null where user_id = %s", (user_id,))
        return
    hi = end if end > start else end + 24
    await conn.execute(
        "update budget_settings set quiet_hours = int4range(%s, %s) where user_id = %s", (start, hi, user_id)
    )


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
