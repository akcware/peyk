"""source_cursor: last-seen position per (user, source). Used by telegram offset and reconcile."""
from __future__ import annotations

from uuid import UUID

import psycopg


async def get(conn: psycopg.AsyncConnection, user_id: UUID, source: str) -> str | None:
    cur = await conn.execute(
        "select cursor from source_cursor where user_id = %s and source = %s", (user_id, source)
    )
    row = await cur.fetchone()
    return row["cursor"] if row else None


async def set(conn: psycopg.AsyncConnection, user_id: UUID, source: str, cursor: str) -> None:
    await conn.execute(
        """
        insert into source_cursor (user_id, source, cursor) values (%s, %s, %s)
        on conflict (user_id, source) do update set cursor = excluded.cursor, updated_at = now()
        """,
        (user_id, source, cursor),
    )
