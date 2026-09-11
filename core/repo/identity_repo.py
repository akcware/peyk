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


async def search(conn: psycopg.AsyncConnection, user_id: UUID, query: str, *, limit: int = 10) -> list[dict]:
    """People we have seen: match display_name or identity value (case-insensitive, any token)."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    clauses = " and ".join("(coalesce(p.display_name,'') ilike %s or i.value ilike %s)" for _ in terms)
    params: list = [user_id]
    for t in terms:
        params += [f"%{t}%", f"%{t}%"]
    cur = await conn.execute(
        f"""
        select p.display_name as name, i.kind, i.value from identity i join person p on p.id = i.person_id
        where i.user_id = %s and {clauses} order by p.display_name nulls last limit %s
        """,
        (*params, limit),
    )
    return await cur.fetchall()
