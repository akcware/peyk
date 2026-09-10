"""person / identity upserts. resolve() is the only entry point workers need."""
from __future__ import annotations

from uuid import UUID

import psycopg

from core.identity import normalize


async def resolve(
    conn: psycopg.AsyncConnection,
    user_id: UUID,
    kind: str,
    raw_value: str,
    *,
    display_name: str | None = None,
    default_region: str = "DE",
) -> UUID:
    """Return the person_id for (kind, value); create person + identity if unknown."""
    value = normalize(kind, raw_value, default_region)
    cur = await conn.execute(
        "select person_id from identity where user_id = %s and kind = %s and value = %s",
        (user_id, kind, value),
    )
    row = await cur.fetchone()
    if row:
        if display_name:
            await conn.execute(
                "update person set display_name = coalesce(display_name, %s) where id = %s",
                (display_name, row["person_id"]),
            )
        return row["person_id"]

    async with conn.transaction():
        cur = await conn.execute(
            "insert into person (user_id, display_name) values (%s, %s) returning id",
            (user_id, display_name),
        )
        person_id = (await cur.fetchone())["id"]
        cur = await conn.execute(
            """
            insert into identity (user_id, person_id, kind, value)
            values (%s, %s, %s, %s)
            on conflict (user_id, kind, value) do update set confidence = identity.confidence
            returning person_id
            """,
            (user_id, person_id, kind, value),
        )
        winner = (await cur.fetchone())["person_id"]
        if winner != person_id:  # lost a race; drop the orphan person
            await conn.execute("delete from person where id = %s", (person_id,))
        return winner


async def link(conn: psycopg.AsyncConnection, user_id: UUID, person_id: UUID, kind: str, raw_value: str) -> None:
    """Attach an additional identity to an existing person (used by /link in phase 6)."""
    value = normalize(kind, raw_value)
    await conn.execute(
        """
        insert into identity (user_id, person_id, kind, value) values (%s, %s, %s, %s)
        on conflict (user_id, kind, value) do update set person_id = excluded.person_id
        """,
        (user_id, person_id, kind, value),
    )
