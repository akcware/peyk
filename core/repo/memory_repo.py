"""memory table: insert + cosine top-k. No dedup, no decay, no consolidation — on purpose."""
from __future__ import annotations

from uuid import UUID

import psycopg

from core.embeddings import to_pgvector


async def insert(conn: psycopg.AsyncConnection, user_id: UUID, text: str, embedding: list[float],
                 source_observation_id: UUID | None = None) -> UUID:
    cur = await conn.execute(
        "insert into memory (user_id, text, embedding, source_observation_id) values (%s, %s, %s::vector, %s) returning id",
        (user_id, text, to_pgvector(embedding), source_observation_id),
    )
    return (await cur.fetchone())["id"]


async def search(conn: psycopg.AsyncConnection, user_id: UUID, embedding: list[float], *, k: int = 5) -> list[dict]:
    cur = await conn.execute(
        """
        select id, text, created_at, 1 - (embedding <=> %s::vector) as score
        from memory where user_id = %s and embedding is not null
        order by embedding <=> %s::vector limit %s
        """,
        (to_pgvector(embedding), user_id, to_pgvector(embedding), k),
    )
    return await cur.fetchall()


async def count(conn: psycopg.AsyncConnection, user_id: UUID) -> int:
    cur = await conn.execute("select count(*) as n from memory where user_id = %s", (user_id,))
    return (await cur.fetchone())["n"]
