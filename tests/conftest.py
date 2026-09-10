"""Shared fixtures. DB tests run against TEST_DATABASE_URL (compose: postgresql://agent:agent@localhost:5433/agent_test).
Schema is rebuilt once per session from db/migrations; tables are truncated before every test."""
from __future__ import annotations

import os
from uuid import UUID

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row

from core import db
from core.config import Settings
from db.migrate import migrate

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://agent:agent@localhost:5433/agent_test")
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
TABLES = ("scheduled_job", "triage", "sent_notification", "mute_rule", "budget_settings", "identity", "person", "action", "source_cursor", "observation")


def _ensure_database(url: str) -> None:
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except psycopg.OperationalError as e:
        if "does not exist" not in str(e):
            raise
        base, _, name = url.rpartition("/")
        with psycopg.connect(f"{base}/postgres", autocommit=True) as c:
            c.execute(f'create database "{name}"')


@pytest.fixture(scope="session")
def test_db_url() -> str:
    _ensure_database(TEST_DB_URL)
    with psycopg.connect(TEST_DB_URL, autocommit=True) as c:
        c.execute("drop schema public cascade; create schema public;")
    import asyncio

    asyncio.run(migrate(TEST_DB_URL))
    return TEST_DB_URL


@pytest.fixture
def settings(test_db_url: str) -> Settings:
    return Settings(
        _env_file=None,
        USER_ID=USER_ID,
        DATABASE_URL=test_db_url,
        COMPOSIO_WEBHOOK_SECRET="whsec_test_secret",
        COMPOSIO_USER_ID="default",
        COMPOSIO_DELIVERY="ws",
        TELEGRAM_BOT_TOKEN="",
    )


@pytest_asyncio.fixture
async def pool(test_db_url: str):
    p = await db.open_pool(test_db_url, min_size=1, max_size=6)
    async with p.connection() as c:
        await c.execute(f"truncate {', '.join(TABLES)} cascade")
    yield p
    await db.close_pool()


@pytest_asyncio.fixture
async def conn(pool):
    async with pool.connection() as c:
        yield c


@pytest_asyncio.fixture
async def raw_conn(test_db_url: str):
    """Independent autocommit connection outside the pool (for concurrency tests)."""
    async with await psycopg.AsyncConnection.connect(test_db_url, autocommit=True, row_factory=dict_row) as c:
        yield c
