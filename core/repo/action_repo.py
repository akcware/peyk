"""action table + the content hash. content_hash = sha256(canonical_json(content)); recomputed at the single
write point (update_content). The approval flow compares approved_hash against content_hash."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

_COLS = "id, user_id, channel, thread_key, content, content_hash, status, approved_hash, external_id, created_at, sent_at"


def canonical_json(content: dict[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(content: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


async def create(conn: psycopg.AsyncConnection, user_id: UUID, *, channel: str, thread_key: str | None,
                 content: dict[str, Any]) -> dict:
    cur = await conn.execute(
        f"""
        insert into action (user_id, channel, thread_key, content, content_hash)
        values (%s, %s, %s, %s, %s) returning {_COLS}
        """,
        (user_id, channel, thread_key, Jsonb(content), content_hash(content)),
    )
    return await cur.fetchone()


async def get(conn: psycopg.AsyncConnection, action_id: UUID) -> dict | None:
    cur = await conn.execute(f"select {_COLS} from action where id = %s", (action_id,))
    return await cur.fetchone()


async def update_content(conn: psycopg.AsyncConnection, action_id: UUID, content: dict[str, Any]) -> dict:
    """The only place content changes. Hash is recomputed and the action goes back to awaiting_approval."""
    cur = await conn.execute(
        f"""
        update action set content = %s, content_hash = %s, status = 'awaiting_approval', approved_hash = null
        where id = %s and status in ('draft', 'awaiting_approval', 'approved', 'failed')
        returning {_COLS}
        """,
        (Jsonb(content), content_hash(content), action_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise ValueError("action cannot be edited in its current status")
    return row


async def set_status(conn: psycopg.AsyncConnection, action_id: UUID, status: str, *, approved_hash: str | None = None,
                     external_id: str | None = None, sent: bool = False) -> dict:
    cur = await conn.execute(
        f"""
        update action set status = %s,
          approved_hash = coalesce(%s, approved_hash),
          external_id = coalesce(%s, external_id),
          sent_at = case when %s then now() else sent_at end
        where id = %s returning {_COLS}
        """,
        (status, approved_hash, external_id, sent, action_id),
    )
    return await cur.fetchone()


async def list_open(conn: psycopg.AsyncConnection, user_id: UUID) -> list[dict]:
    cur = await conn.execute(
        f"select {_COLS} from action where user_id = %s and status in ('draft','awaiting_approval','approved','failed') order by created_at desc",
        (user_id,),
    )
    return await cur.fetchall()


# ---- chat_state: pending edit per thread ----

async def set_pending_edit(conn: psycopg.AsyncConnection, user_id: UUID, thread_key: str, action_id: UUID | None) -> None:
    await conn.execute(
        """
        insert into chat_state (user_id, thread_key, pending_edit_action_id) values (%s, %s, %s)
        on conflict (user_id, thread_key) do update set pending_edit_action_id = excluded.pending_edit_action_id, updated_at = now()
        """,
        (user_id, thread_key, action_id),
    )


async def get_pending_edit(conn: psycopg.AsyncConnection, user_id: UUID, thread_key: str) -> UUID | None:
    cur = await conn.execute(
        "select pending_edit_action_id from chat_state where user_id = %s and thread_key = %s", (user_id, thread_key)
    )
    row = await cur.fetchone()
    return row["pending_edit_action_id"] if row else None
