"""Connection pool + transaction helper. All SQL lives in core/repo; this module only hands out connections."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def open_pool(database_url: str, *, min_size: int = 1, max_size: int = 8) -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=False,
        )
        await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("db pool not opened; call core.db.open_pool() first")
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[psycopg.AsyncConnection]:
    """Autocommit connection from the pool."""
    async with pool().connection() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[psycopg.AsyncConnection]:
    """Explicit transaction; commits on success, rolls back on exception."""
    async with pool().connection() as conn, conn.transaction():
        yield conn
