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
from core.repo import cursor_repo, observation_repo, user_repo

log = get_logger("workers.ticks")


@dataclass
class TickContext:
    settings: Settings
    registry: AdapterRegistry | None
    notifier: Any = None              # single-user Notifier (tests) — or use `notifiers` for per-user lookup
    retriage: Callable[[psycopg.AsyncConnection, Observation], Awaitable[Any]] | None = None
    notifiers: Any = None             # workers.users.Notifiers
    agent: Any = None                 # AgentClient, for agent-voiced reactions to system events
    embedder: Any = None
    background: bool = False          # run slow handlers (first_learn) detached from the consumer loop

    async def notifier_for(self, conn, user_id) -> Any:
        if self.notifiers is not None:
            return await self.notifiers.for_user(conn, user_id)
        return self.notifier


# ---------- morning brief ----------

async def brief_items(conn: psycopg.AsyncConnection, user_id, since: datetime, *, min_urgency: int = 3) -> list[dict]:
    """Important observations since `since`, minus what the person wrote themselves and minus threads they have
    already answered (a later message of theirs in the same thread). `notified_at` is set when we already told them."""
    cur = await conn.execute(
        """
        select o.id, o.source, o.payload, t.urgency, t.category, t.reason, t.summary, o.occurred_at,
               (select min(sn.sent_at) from sent_notification sn where sn.observation_id = o.id) as notified_at
        from observation o join triage t on t.observation_id = o.id
        where o.user_id = %s and o.occurred_at >= %s and t.urgency >= %s and o.is_backfill = false
          and o.kind <> 'message_out' and not coalesce(o.payload->'label_ids' ? 'SENT', false)
          and not exists (
            select 1 from observation r
            where r.user_id = o.user_id and r.source = o.source and r.thread_key = o.thread_key and r.occurred_at > o.occurred_at
              and (r.kind = 'message_out' or coalesce(r.payload->'label_ids' ? 'SENT', false)))
        order by t.urgency desc, o.occurred_at desc
        limit 30
        """,
        (user_id, since, min_urgency),
    )
    return await cur.fetchall()


def when_label(at: datetime, now: datetime) -> str:
    """'today 08:12 (1h ago)' / 'yesterday 17:30 (15h ago)' in the timezone of `now`."""
    local = at.astimezone(now.tzinfo)
    hours = max(0, int((now - at).total_seconds() // 3600))
    ago = f"{hours}h ago" if hours < 48 else f"{hours // 24}d ago"
    if local.date() == now.date():
        day = "today"
    elif (now.date() - local.date()).days == 1:
        day = "yesterday"
    else:
        day = f"{local:%a %d %b}"
    return f"{day} {local:%H:%M} ({ago})"


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
        lines.append(f"   {it.get('summary') or it['reason']}")
    lines += ["", f"{len(items)} item(s) with urgency ≥ 3."]
    return "\n".join(lines)


def brief_event_text(items: list[dict], now: datetime) -> str:
    """The system event handed to the agent: the important observations of the last 24h, compactly."""
    if not items:
        return f"it is morning ({now:%a %d %b}); nothing with urgency >= 3 arrived in the last 24h — give the person a one-line good-morning brief saying it is quiet"
    lines = [f"it is morning ({now:%a %d %b %H:%M}); write the person's morning brief from these {len(items)} items of the last 24h "
             "(urgency 1-5; each says when it arrived and whether you already told the person about it):"]
    for it in items:
        p = it["payload"]
        who = p.get("from") or p.get("summary") or it["source"]
        subject = p.get("subject") or p.get("summary") or ""
        when = when_label(it["occurred_at"], now)
        told = f"; you told them at {it['notified_at'].astimezone(now.tzinfo):%H:%M}" if it.get("notified_at") else ""
        lines.append(f"- [{it['source']}] u{it['urgency']} {when}{told} — {who} — {subject}: {it.get('summary') or it['reason']}")
    lines += [
        "Rules: address the person by first name. Say when things came in — never present yesterday's mail as if it just arrived.",
        "Verification codes, one-time passwords and login/password-setup links expire within minutes: when such an item is older "
        "than an hour, do not tell the person to use it — leave it out, or at most say a fresh one can be requested.",
        "Mail the person already answered is not in this list; what you already told them about is a reminder, not news.",
        "Group by what needs action today vs. what can wait; 4-8 short lines; no bullets with raw headers; end with one sentence on what you would do first",
    ]
    return "\n".join(lines)


async def morning_brief(conn, obs: Observation, ctx: TickContext) -> None:
    now = local_now(await user_repo.get(conn, obs.user_id), ctx.settings)
    items = await brief_items(conn, obs.user_id, now - timedelta(hours=24))
    notifier = await ctx.notifier_for(conn, obs.user_id)
    fallback = render_brief(items, now)
    if ctx.agent is not None and ctx.embedder is not None:
        from workers import chat

        sent = await chat.react_to_event(conn, obs.user_id, brief_event_text(items, now), settings=ctx.settings, agent=ctx.agent,
                                         notifier=notifier, embedder=ctx.embedder, registry=ctx.registry, fallback=fallback,
                                         instruction="write the brief now")
        if sent:
            return
    await notifier.send_text(fallback)


def local_now(user: dict | None, settings: Settings) -> datetime:
    """Now in the person's timezone (falls back to the operator's TIMEZONE, then UTC)."""
    from zoneinfo import ZoneInfo

    for tz in ((user or {}).get("timezone"), getattr(settings, "TIMEZONE", None)):
        if tz:
            try:
                return datetime.now(tz=ZoneInfo(str(tz)))
            except Exception:  # noqa: BLE001 - unknown zone name
                continue
    return datetime.now(tz=UTC)


# ---------- followup ----------

async def followup(conn, obs: Observation, ctx: TickContext) -> None:
    note = (obs.payload.get("job") or {}).get("payload", {}).get("note") or "(empty reminder)"
    await (await ctx.notifier_for(conn, obs.user_id)).send_text(f"⏰ Reminder: {note}")


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
        if cursor is None:
            # Bootstrap: establish the cursor only. Replaying history here would triage (and notify about) old mail.
            await cursor_repo.set(conn, obs.user_id, adapter.id, now.isoformat())
            log.info("reconcile.bootstrap_cursor", adapter=adapter.id, cursor=now.isoformat())
            continue
        since = datetime.fromisoformat(cursor) - lookback
        newest = datetime.fromisoformat(cursor)
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
        if newest > datetime.fromisoformat(cursor):
            await cursor_repo.set(conn, obs.user_id, adapter.id, newest.isoformat())
        log.info("reconcile.done", adapter=adapter.id, inserted=inserted, already_seen=skipped, since=since.isoformat())


async def await_connection(conn, obs: Observation, ctx: TickContext) -> None:
    from workers import onboarding

    payload = (obs.payload.get("job") or {}).get("payload", {})
    notifier = await ctx.notifier_for(conn, obs.user_id)
    react = None
    if ctx.agent is not None and ctx.embedder is not None:
        from workers import chat

        async def react(c, user_id, event_text, fallback):
            await chat.react_to_event(c, user_id, event_text, settings=ctx.settings, agent=ctx.agent, notifier=notifier,
                                      embedder=ctx.embedder, registry=ctx.registry, fallback=fallback)
    await onboarding.check_connection(conn, obs.user_id, payload, ctx.registry, notifier, react=react)


async def first_learn(conn, obs: Observation, ctx: TickContext) -> None:
    """Slow (metadata sampling + one big model call): detached so it never stalls other users' observations."""
    import asyncio

    from core import db
    from workers import learn

    toolkit = (obs.payload.get("job") or {}).get("payload", {}).get("toolkit")
    if not toolkit or ctx.agent is None:
        return
    notifier = await ctx.notifier_for(conn, obs.user_id)
    if not ctx.background:
        await learn.run(conn, obs.user_id, toolkit, registry=ctx.registry, agent=ctx.agent, notifier=notifier, embedder=ctx.embedder)
        return

    async def _detached():
        try:
            async with db.connection() as c:
                await learn.run(c, obs.user_id, toolkit, registry=ctx.registry, agent=ctx.agent, notifier=notifier, embedder=ctx.embedder)
        except Exception as e:  # noqa: BLE001
            log.error("first_learn.failed", user_id=str(obs.user_id), toolkit=toolkit, error=str(e))

    asyncio.create_task(_detached(), name=f"first_learn:{obs.user_id}")


HANDLERS: dict[str, Callable[[psycopg.AsyncConnection, Observation, TickContext], Awaitable[None]]] = {
    "first_learn": first_learn,
    "morning_brief": morning_brief,
    "followup": followup,
    "recheck_thread": recheck_thread,
    "reconcile": reconcile,
    "await_connection": await_connection,
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
