"""Tiny migration runner: applies db/migrations/NNNN_*.sql in order, records them in schema_migrations."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def migrate(database_url: str) -> list[str]:
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=False) as conn:
        await conn.execute(
            "create table if not exists schema_migrations (version text primary key, applied_at timestamptz default now())"
        )
        await conn.commit()
        cur = await conn.execute("select version from schema_migrations")
        done = {r[0] for r in await cur.fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            await conn.execute(path.read_text())
            await conn.execute("insert into schema_migrations (version) values (%s)", (version,))
            await conn.commit()
            applied.append(version)
    return applied


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set")
    for v in asyncio.run(migrate(url)):
        print(f"applied {v}")
