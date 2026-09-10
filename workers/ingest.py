"""One asyncio task per ingestable adapter: `async for obs in adapter.subscribe(conn): insert(obs)`.
Dedup is in the DB (unique + ON CONFLICT DO NOTHING). Adapter failures restart with exponential backoff."""
from __future__ import annotations

import asyncio
from uuid import UUID

from core import db
from core.adapter import SourceAdapter
from core.log import get_logger
from core.repo import observation_repo

log = get_logger("workers.ingest")


async def run_adapter(adapter: SourceAdapter, user_id: UUID, *, max_backoff: float = 60.0) -> None:
    backoff = 1.0
    while True:
        try:
            conn = await adapter.connect(user_id)
            async for obs in adapter.subscribe(conn):
                async with db.connection() as dbc:
                    stored = await observation_repo.insert(dbc, obs)
                log.info(
                    "ingest.observation",
                    adapter=adapter.id, source=obs.source, source_key=obs.source_key,
                    duplicate=stored is None, observation_id=str(stored.id) if stored else None,
                )
                backoff = 1.0
            log.info("ingest.subscribe_ended", adapter=adapter.id)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - loop must not die
            log.error("ingest.adapter_error", adapter=adapter.id, error=str(e), retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


def start_ingest_tasks(adapters: list[SourceAdapter], user_id: UUID) -> list[asyncio.Task]:
    return [asyncio.create_task(run_adapter(a, user_id), name=f"ingest:{a.id}") for a in adapters]
