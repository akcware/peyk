"""Starts all worker loops as asyncio tasks. Long-lived process; not deployed to AgentCore."""
from __future__ import annotations

import asyncio
import signal

from agent.client import AgentClient
from core import db
from core.adapter import AdapterRegistry
from core.config import get_settings
from core.log import configure_logging, get_logger
from workers import ingest, maintenance, scheduler, ticks, triage

log = get_logger("workers.main")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    await db.open_pool(settings.DATABASE_URL)
    registry = AdapterRegistry.from_ids(settings.adapter_ids)
    log.info("workers.start", adapters=[a.id for a in registry.all()], ingest=[a.id for a in registry.ingestable()])

    tasks = ingest.start_ingest_tasks(registry.ingestable(), settings.USER_ID)
    notifier = triage.Notifier(registry.get(settings.CONTROL_SOURCE), settings.TELEGRAM_CHAT_ID, settings.USER_ID)
    agent = AgentClient(settings.AGENT_MODE, runtime_arn=settings.AGENTCORE_RUNTIME_ARN, region=settings.AWS_REGION)
    tick_ctx = ticks.TickContext(settings=settings, registry=registry, notifier=notifier)
    tasks.append(asyncio.create_task(triage.run(settings, agent=agent, notifier=notifier, tick_ctx=tick_ctx), name="triage"))
    tasks.append(asyncio.create_task(scheduler.run(settings), name="scheduler"))
    tasks.append(asyncio.create_task(maintenance.recover_loop(settings.USER_ID), name="recover"))

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
