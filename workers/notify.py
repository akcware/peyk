"""Phase 0 consumer: claim_next() -> raw Telegram notification for gmail -> complete().

The `source == 'gmail'` check below is *temporary*: phase 1 replaces this worker with triage + gate,
and no source-specific branch survives in workers/ after that.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from core import db, queue
from core.adapter import SourceAdapter
from core.identity import display_name_from_header
from core.log import get_logger
from core.models import Connection, Content, Observation
from core.repo import identity_repo

log = get_logger("workers.notify")


def render_raw_notification(obs: Observation) -> str:
    p = obs.payload
    head = f"[{obs.source}] {p.get('from', '?')}: {p.get('subject', '(no subject)')}"
    snippet = (p.get("snippet") or p.get("text") or "").strip()
    return f"{head}\n{snippet[:300]}" if snippet else head


_tg_conn: Connection | None = None


async def _telegram_conn(telegram: SourceAdapter, user_id: UUID) -> Connection:
    global _tg_conn
    if _tg_conn is None:
        _tg_conn = await telegram.connect(user_id)
    return _tg_conn


async def handle(obs: Observation, *, telegram: SourceAdapter, chat_id: str) -> None:
    async with db.connection() as conn:
        sender = obs.payload.get("from")
        if sender and obs.kind == "message_in" and obs.source == "gmail":
            await identity_repo.resolve(conn, obs.user_id, "email", sender, display_name=display_name_from_header(sender))
    if obs.source == "gmail" and chat_id:  # TEMPORARY (phase 0 only) — see module docstring
        conn_handle = await _telegram_conn(telegram, obs.user_id)
        await telegram.send(conn_handle, chat_id, Content(text=render_raw_notification(obs)))


async def run(user_id: UUID, *, telegram: SourceAdapter, chat_id: str, idle_sleep: float = 1.0) -> None:
    while True:
        async with db.connection() as conn:
            obs = await queue.claim_next(conn, user_id)
        if obs is None:
            await asyncio.sleep(idle_sleep)
            continue
        slog = log.bind(observation_id=str(obs.id), source=obs.source)
        try:
            await handle(obs, telegram=telegram, chat_id=chat_id)
            async with db.connection() as conn:
                await queue.complete(conn, obs.id)
            slog.info("notify.done")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            async with db.connection() as conn:
                status = await queue.fail(conn, obs.id)
            slog.error("notify.failed", error=str(e), status=status, attempts=obs.attempts)
            await asyncio.sleep(min(2 ** obs.attempts, 30))  # back off before the next claim


async def recover_loop(user_id: UUID, *, every: float = 60.0) -> None:
    while True:
        await asyncio.sleep(every)
        async with db.connection() as conn:
            n = await queue.recover_stale(conn, user_id)
        if n:
            log.warning("queue.recovered_stale", count=n)
