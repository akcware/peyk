"""Scheduler: every 30s, claim due jobs and turn each into a `tick` observation. It does no work itself —
ticks go through the same queue as every other observation, so the scheduler is just another source.
Recurring jobs get their next `pending` row here."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from core import db
from core.config import Settings
from core.log import get_logger
from core.models import Observation
from core.recurrence import next_run
from core.repo import job_repo, observation_repo

log = get_logger("workers.scheduler")


def tick_observation(job: dict) -> Observation:
    return Observation(
        user_id=job["user_id"], source="system", source_key=str(job["id"]), kind="tick",
        occurred_at=job["run_at"], thread_key=None,
        payload={"job": {"id": str(job["id"]), "kind": job["kind"], "payload": job["payload"],
                         "recurrence": job["recurrence"], "created_by": job["created_by"],
                         "run_at": job["run_at"].isoformat()}},
    )


async def fire_due(conn, user_id: UUID, now: datetime, tz: str) -> int:
    jobs = await job_repo.claim_due(conn, user_id, now)
    for job in jobs:
        try:
            await observation_repo.insert(conn, tick_observation(job))
            await job_repo.mark(conn, job["id"], "done")
            if job["recurrence"]:
                await job_repo.create(conn, user_id, run_at=next_run(job["recurrence"], now, tz), kind=job["kind"],
                                      payload=job["payload"], recurrence=job["recurrence"], created_by=job["created_by"])
            log.info("scheduler.fired", job_id=str(job["id"]), kind=job["kind"])
        except Exception as e:  # noqa: BLE001
            await job_repo.mark(conn, job["id"], "failed")
            log.error("scheduler.fire_failed", job_id=str(job["id"]), error=str(e))
    return len(jobs)


async def ensure_default_jobs(conn, settings: Settings, now: datetime | None = None) -> None:
    """Idempotent: creates the recurring system jobs (morning brief, reconcile) if none is pending."""
    now = now or datetime.now(tz=UTC)
    if settings.MORNING_BRIEF_AT and not await job_repo.pending_of_kind(conn, settings.USER_ID, "morning_brief"):
        rec = f"daily@{settings.MORNING_BRIEF_AT}"
        await job_repo.create(conn, settings.USER_ID, run_at=next_run(rec, now, settings.TIMEZONE), kind="morning_brief",
                              recurrence=rec, created_by="system")
    if settings.RECONCILE_EVERY and not await job_repo.pending_of_kind(conn, settings.USER_ID, "reconcile"):
        rec = f"every:{settings.RECONCILE_EVERY}"
        await job_repo.create(conn, settings.USER_ID, run_at=next_run(rec, now, settings.TIMEZONE), kind="reconcile",
                              recurrence=rec, created_by="system")


async def run(settings: Settings, *, every: float = 30.0) -> None:
    async with db.connection() as conn:
        await ensure_default_jobs(conn, settings)
    while True:
        try:
            async with db.connection() as conn:
                await fire_due(conn, settings.USER_ID, datetime.now(tz=UTC), settings.TIMEZONE)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("scheduler.loop_error", error=str(e))
        await asyncio.sleep(every)
