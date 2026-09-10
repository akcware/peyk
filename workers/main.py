"""Starts all worker loops as asyncio tasks. Long-lived process; not deployed to AgentCore."""
from __future__ import annotations

import asyncio
import signal

from core import db
from core.adapter import AdapterRegistry
from core.config import get_settings
from core.log import configure_logging, get_logger
from workers import ingest, notify

log = get_logger("workers.main")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    await db.open_pool(settings.DATABASE_URL)
    registry = AdapterRegistry.from_ids(settings.adapter_ids)
    log.info("workers.start", adapters=[a.id for a in registry.all()], ingest=[a.id for a in registry.ingestable()])

    tasks = ingest.start_ingest_tasks(registry.ingestable(), settings.USER_ID)
    tasks.append(asyncio.create_task(
        notify.run(settings.USER_ID, telegram=registry.get("telegram"), chat_id=settings.TELEGRAM_CHAT_ID),
        name="notify",
    ))
    tasks.append(asyncio.create_task(notify.recover_loop(settings.USER_ID), name="recover"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("workers.stopping")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
