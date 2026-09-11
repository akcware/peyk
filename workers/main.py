"""Starts all worker loops as asyncio tasks. Long-lived process; not deployed to AgentCore."""
from __future__ import annotations

import asyncio
import signal

from agent.client import AgentClient
from core import db
from core.adapter import AdapterRegistry
from core.config import export_agent_env, get_settings
from core.embeddings import TitanEmbedder
from core.log import configure_logging, get_logger
from workers import approval, ingest, maintenance, scheduler, ticks, triage, users

log = get_logger("workers.main")


async def main() -> None:
    settings = get_settings()
    export_agent_env(settings)
    configure_logging(settings.LOG_LEVEL)
    await db.open_pool(settings.DATABASE_URL)
    directory = users.DbUserDirectory(settings)
    await users.ensure_bootstrap_user(settings)
    registry = AdapterRegistry.from_ids(settings.adapter_ids, composio={"users": directory}, telegram={"users": directory})
    log.info("workers.start", adapters=[a.id for a in registry.all()], ingest=[a.id for a in registry.ingestable()])

    tasks = ingest.start_ingest_tasks(registry.ingestable(), settings.USER_ID)
    notifiers = users.Notifiers(registry, settings)
    agent = AgentClient(settings.AGENT_MODE, runtime_arn=settings.AGENTCORE_RUNTIME_ARN, region=settings.AWS_REGION)
    embedder = TitanEmbedder(region=settings.AWS_REGION)
    tick_ctx = ticks.TickContext(settings=settings, registry=registry, notifiers=notifiers, agent=agent, embedder=embedder, background=True)
    flow = approval.ApprovalFlow(registry, None)   # notifier is set per observation
    tasks.append(asyncio.create_task(
        triage.run(settings, agent=agent, tick_ctx=tick_ctx, embedder=embedder, approval=flow, notifiers=notifiers), name="triage"))
    tasks.append(asyncio.create_task(scheduler.run(settings), name="scheduler"))
    tasks.append(asyncio.create_task(maintenance.recover_loop(None), name="recover"))

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
