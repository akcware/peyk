"""app_user SQL. Users are created lazily on first contact through a control channel."""
from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

_COLS = "id, control_source, control_thread_key, composio_user_id, display_name, profile, language, timezone, state, created_at"


async def get(conn: psycopg.AsyncConnection, user_id: UUID) -> dict | None:
    cur = await conn.execute(f"select {_COLS} from app_user where id = %s", (user_id,))
    return await cur.fetchone()


async def get_by_control(conn: psycopg.AsyncConnection, source: str, thread_key: str) -> dict | None:
    cur = await conn.execute(
        f"select {_COLS} from app_user where control_source = %s and control_thread_key = %s", (source, thread_key)
    )
    return await cur.fetchone()


async def get_by_composio(conn: psycopg.AsyncConnection, composio_user_id: str) -> dict | None:
    cur = await conn.execute(f"select {_COLS} from app_user where composio_user_id = %s", (composio_user_id,))
    return await cur.fetchone()


async def create(
    conn: psycopg.AsyncConnection, *, control_source: str, control_thread_key: str, user_id: UUID | None = None,
    composio_user_id: str | None = None, display_name: str | None = None, profile: str = "", language: str = "en",
    timezone: str = "UTC",
) -> dict:
    cur = await conn.execute(
        f"""
        insert into app_user (id, control_source, control_thread_key, composio_user_id, display_name, profile, language, timezone)
        values (coalesce(%s, gen_random_uuid()), %s, %s, %s, %s, %s, %s, %s)
        on conflict (control_source, control_thread_key) do update set display_name = coalesce(app_user.display_name, excluded.display_name)
        returning {_COLS}
        """,
        (user_id, control_source, control_thread_key, composio_user_id, display_name, profile, language, timezone),
    )
    row = await cur.fetchone()
    if row["composio_user_id"] is None:  # default: our own uuid is the Composio entity id
        cur = await conn.execute(
            f"update app_user set composio_user_id = %s where id = %s returning {_COLS}", (str(row["id"]), row["id"])
        )
        row = await cur.fetchone()
    return row


async def get_or_create_by_control(
    conn: psycopg.AsyncConnection, source: str, thread_key: str, *, display_name: str | None = None, language: str = "en",
    timezone: str = "UTC",
) -> tuple[dict, bool]:
    """Returns (user, created)."""
    existing = await get_by_control(conn, source, thread_key)
    if existing:
        return existing, False
    return await create(conn, control_source=source, control_thread_key=thread_key, display_name=display_name,
                        language=language, timezone=timezone), True


async def update(conn: psycopg.AsyncConnection, user_id: UUID, **fields: Any) -> dict:
    allowed = {"display_name", "profile", "language", "timezone", "composio_user_id"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = %s")
            params.append(v)
    if not sets:
        return await get(conn, user_id)
    params.append(user_id)
    cur = await conn.execute(f"update app_user set {', '.join(sets)} where id = %s returning {_COLS}", params)
    return await cur.fetchone()


async def merge_state(conn: psycopg.AsyncConnection, user_id: UUID, patch: dict[str, Any]) -> dict:
    cur = await conn.execute(
        f"update app_user set state = state || %s where id = %s returning {_COLS}", (Jsonb(patch), user_id)
    )
    return await cur.fetchone()


async def list_all(conn: psycopg.AsyncConnection) -> list[dict]:
    cur = await conn.execute(f"select {_COLS} from app_user order by created_at")
    return await cur.fetchall()


async def ensure_bootstrap(conn: psycopg.AsyncConnection, *, user_id: UUID, control_source: str, control_thread_key: str,
                           composio_user_id: str, profile: str, language: str, timezone: str) -> dict | None:
    """Migrates the single-user .env configuration into app_user once. No-op if the chat id is empty."""
    if not control_thread_key:
        return None
    existing = await get(conn, user_id) or await get_by_control(conn, control_source, control_thread_key)
    if existing:
        return existing
    if profile.lower().startswith("synthetic"):   # never seed a person with an example profile
        profile = ""
    return await create(conn, control_source=control_source, control_thread_key=control_thread_key, user_id=user_id,
                        composio_user_id=composio_user_id or str(user_id), profile=profile, language=language, timezone=timezone)


PURGE_TABLES = ("sent_notification", "mute_rule", "budget_settings", "memory", "scheduled_job", "chat_state", "action",
                "identity", "person", "observation")


async def purge(conn: psycopg.AsyncConnection, user_id: UUID, *, keep_user: bool = False) -> dict[str, int]:
    """Right-to-erasure: delete every row keyed by this user, then (unless keep_user) the app_user row."""
    counts: dict[str, int] = {}
    cur = await conn.execute("delete from triage where observation_id in (select id from observation where user_id = %s)", (user_id,))
    counts["triage"] = cur.rowcount
    for t in PURGE_TABLES:
        cur = await conn.execute(f"delete from {t} where user_id = %s", (user_id,))
        counts[t] = cur.rowcount
    if keep_user:
        await conn.execute("update app_user set state = '{}'::jsonb where id = %s", (user_id,))
    else:
        cur = await conn.execute("delete from app_user where id = %s", (user_id,))
        counts["app_user"] = cur.rowcount
    return counts
