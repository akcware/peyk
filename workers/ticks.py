"""Handlers for `tick` observations (scheduled jobs). Dispatch is a dict keyed by job kind — data, not
source branches. Each handler gets the tick observation and a context with the things it may need."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from core.adapter import AdapterRegistry
from core.config import Settings
from core.log import get_logger
from core.models import Content, Observation
from core.repo import cursor_repo, observation_repo

log = get_logger("workers.ticks")


@dataclass
class TickContext:
    settings: Settings
    registry: AdapterRegistry | None
    notifier: Any                     # workers.triage.Notifier (send_text / send)
    retriage: Callable[[psycopg.AsyncConnection, Observation], Awaitable[Any]] | None = None


# ---------- morning brief ----------

async def brief_items(conn: psycopg.AsyncConnection, user_id, since: datetime, *, min_urgency: int = 3) -> list[dict]:
    cur = await conn.execute(
        """
        select o.source, o.payload, t.urgency, t.category, t.reason, o.occurred_at
        from observation o join triage t on t.observation_id = o.id
        where o.user_id = %s and o.occurred_at >= %s and t.urgency >= %s and o.is_backfill = false
        order by t.urgency desc, o.occurred_at desc
        limit 30
        """,
        (user_id, since, min_urgency),
    )
    return await cur.fetchall()


def render_brief(items: list[dict], now: datetime) -> str:
    if not items:
        return f"☀️ Morning brief — {now:%a %d %b}\n\nNothing important in the last 24h. Enjoy the quiet."
    lines = [f"☀️ Morning brief — {now:%a %d %b}", ""]
    for it in items:
        p = it["payload"]
        who = p.get("from") or p.get("summary") or it["source"]
        subject = p.get("subject") or p.get("summary") or "(no subject)"
        mark = "‼️" if it["urgency"] >= 5 else "❗" if it["urgency"] == 4 else "•"
        lines.append(f"{mark} [{it['source']}] {who} — {subject}")
        lines.append(f"   {it['reason']}")
    lines += ["", f"{len(items)} item(s) with urgency ≥ 3."]
    return "\n".join(lines)


async def morning_brief(conn, obs: Observation, ctx: TickContext) -> None:
    now = datetime.now(tz=UTC)
    items = await brief_items(conn, obs.user_id, now - timedelta(hours=24))
    await ctx.notifier.send_text(render_brief(items, now))


# ---------- followup ----------

async def followup(conn, obs: Observation, ctx: TickContext) -> None:
    note = (obs.payload.get("job") or {}).get("payload", {}).get("note") or "(empty reminder)"
    await ctx.notifier.send_text(f"⏰ Reminder: {note}")


# ---------- recheck_thread ----------

async def recheck_thread(conn, obs: Observation, ctx: TickContext) -> None:
    thread_key = (obs.payload.get("job") or {}).get("payload", {}).get("thread_key")
    if not thread_key or ctx.retriage is None:
        return
    last = await observation_repo.list_by_thread(conn, obs.user_id, thread_key, limit=1, kinds=("message_in",))
    if last:
        await ctx.retriage(conn, last[0])


# ---------- reconcile (Composio safety net) ----------

async def reconcile(conn, obs: Observation, ctx: TickContext) -> None:
    """For every ingestable adapter: backfill(since = cursor - lookback) with is_backfill=False and insert
    ON CONFLICT DO NOTHING. Missed trigger events surface as fresh observations; seen ones are no-ops."""
    if ctx.registry is None:
        return
    lookback = timedelta(minutes=ctx.settings.RECONCILE_LOOKBACK_MINUTES)
    now = datetime.now(tz=UTC)
    for adapter in ctx.registry.ingestable():
        cursor = await cursor_repo.get(conn, obs.user_id, adapter.id)
        since = (datetime.fromisoformat(cursor) if cursor else now - timedelta(hours=24)) - lookback
        newest = datetime.fromisoformat(cursor) if cursor else since
        inserted = skipped = 0
        try:
            handle = await adapter.connect(obs.user_id)
            async for o in adapter.backfill(handle, since, is_backfill=False):
                stored = await observation_repo.insert(conn, o)
                inserted += stored is not None
                skipped += stored is None
                newest = max(newest, o.occurred_at)
        except TypeError:  # adapter.backfill without is_backfill kwarg (telegram/whatsapp stubs)
            continue
        except Exception as e:  # noqa: BLE001
            log.error("reconcile.adapter_failed", adapter=adapter.id, error=str(e))
            continue
        if newest and (not cursor or newest > datetime.fromisoformat(cursor)):
            await cursor_repo.set(conn, obs.user_id, adapter.id, newest.isoformat())
        log.info("reconcile.done", adapter=adapter.id, inserted=inserted, already_seen=skipped, since=since.isoformat())


HANDLERS: dict[str, Callable[[psycopg.AsyncConnection, Observation, TickContext], Awaitable[None]]] = {
    "morning_brief": morning_brief,
    "followup": followup,
    "recheck_thread": recheck_thread,
    "reconcile": reconcile,
}


async def handle_tick(conn, obs: Observation, ctx: TickContext) -> None:
    kind = (obs.payload.get("job") or {}).get("kind")
    handler = HANDLERS.get(kind)
    if handler is None:
        log.warning("tick.unknown_kind", kind=kind)
        return
    await handler(conn, obs, ctx)


def content(text: str) -> Content:
    return Content(text=text)
