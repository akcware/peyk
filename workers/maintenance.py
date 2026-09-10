"""Housekeeping loops: stale-claim recovery."""
from __future__ import annotations

import asyncio
from uuid import UUID

from core import db, queue
from core.log import get_logger

log = get_logger("workers.maintenance")


async def recover_loop(user_id: UUID, *, every: float = 60.0) -> None:
    while True:
        await asyncio.sleep(every)
        async with db.connection() as conn:
            n = await queue.recover_stale(conn, user_id)
        if n:
            log.warning("queue.recovered_stale", count=n)
