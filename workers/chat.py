"""Chat worker: control-channel plain text -> history + pre-fetched context -> AgentClient.chat() -> reply.
The agent gets data in the payload (it has no DB); if it returns a NeedMore intent, we fetch more and
call again — at most MAX_ROUNDS rounds. Side-effect intents are applied here."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent.client import AgentClient
from core.config import Settings
from core.embeddings import Embedder
from core.log import get_logger
from core.models import Observation
from core.repo import job_repo, memory_repo, observation_repo
from core.routing import control_text

log = get_logger("workers.chat")

HISTORY_TURNS = 10
RECENT_HOURS = 48
RECENT_LIMIT = 50
MAX_ROUNDS = 2


def _turn_text(obs: Observation) -> tuple[str, str] | None:
    if obs.kind == "message_out":
        return "assistant", str(obs.payload.get("text") or "")
    text = control_text(obs)
    if not text or text.startswith("/") or "callback_query" in obs.payload:
        return None
    return "user", text


async def build_history(conn: psycopg.AsyncConnection, obs: Observation, *, turns: int = HISTORY_TURNS) -> list[dict[str, str]]:
    rows = await observation_repo.list_by_thread(conn, obs.user_id, obs.thread_key or "", limit=turns * 3 + 5)
    out: list[dict[str, str]] = []
    for r in rows:                     # newest first
        if r.id == obs.id:
            continue
        t = _turn_text(r)
        if t:
            out.append({"role": t[0], "text": t[1]})
        if len(out) >= turns:
            break
    return list(reversed(out))


def compact(obs: Observation, urgency: int | None = None) -> dict[str, Any]:
    p = obs.payload
    d = {"id": str(obs.id), "source": obs.source, "kind": obs.kind, "occurred_at": obs.occurred_at.isoformat(),
         "thread_key": obs.thread_key}
    for k in ("from", "to", "subject", "summary", "location"):
        if p.get(k):
            d[k] = p[k]
    snippet = p.get("snippet") or p.get("text") or ""
    if snippet:
        d["snippet"] = str(snippet)[:240]
    if urgency is not None:
        d["urgency"] = urgency
    return d


async def recent_observations(conn: psycopg.AsyncConnection, user_id: UUID, settings: Settings, *, since: datetime,
                              limit: int = RECENT_LIMIT, query: str | None = None) -> list[dict[str, Any]]:
    if query:
        terms = [t for t in query.lower().split() if t]
        clauses = " and ".join("payload::text ilike %s" for _ in terms)
        cur = await conn.execute(
            f"select id from observation where user_id = %s and occurred_at >= %s and source <> %s and kind <> 'tick' "
            f"and ({clauses}) order by occurred_at desc limit %s",
            (user_id, since, settings.CONTROL_SOURCE, *[f"%{t}%" for t in terms], limit),
        )
        ids = [r["id"] for r in await cur.fetchall()]
        rows = [await observation_repo.get(conn, i) for i in ids]
    else:
        rows = [o for o in await observation_repo.list_since(conn, user_id, since, limit=limit * 2)
                if o.source != settings.CONTROL_SOURCE and o.kind != "tick"][:limit]
    if not rows:
        return []
    cur = await conn.execute("select observation_id, urgency from triage where observation_id = any(%s)", ([o.id for o in rows],))
    urg = {r["observation_id"]: r["urgency"] for r in await cur.fetchall()}
    return [compact(o, urg.get(o.id)) for o in rows]


async def apply_intents(conn: psycopg.AsyncConnection, obs: Observation, intents: list[dict[str, Any]], *,
                        embedder: Embedder, on_draft=None) -> list[str]:
    applied: list[str] = []
    for it in intents:
        kind = it.get("intent")
        try:
            if kind == "MemoryWrite" and it.get("text"):
                vec = await embedder.embed(it["text"])
                await memory_repo.insert(conn, obs.user_id, it["text"], vec, source_observation_id=obs.id)
                applied.append("MemoryWrite")
            elif kind == "ScheduleRequest":
                run_at = datetime.fromisoformat(it["when_iso"])
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=UTC)
                await job_repo.create(conn, obs.user_id, run_at=run_at, kind="followup", payload={"note": it.get("note", "")}, created_by="agent")
                applied.append("ScheduleRequest")
            elif kind == "ActionDraft":
                if on_draft is not None:
                    await on_draft(conn, obs, it)
                    applied.append("ActionDraft")
                else:
                    log.info("chat.draft_ignored_until_phase4", body=str(it.get("body", ""))[:80])
            elif kind == "NeedMore":
                pass  # handled by the round loop
            else:
                log.warning("chat.unknown_intent", intent=kind)
        except Exception as e:  # noqa: BLE001 - one bad intent must not lose the reply
            log.error("chat.intent_failed", intent=kind, error=str(e))
    return applied


async def converse(conn: psycopg.AsyncConnection, obs: Observation, *, settings: Settings, agent: AgentClient,
                   embedder: Embedder, now: datetime | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Runs the (bounded) agent rounds; returns (reply, intents without NeedMore)."""
    now = now or datetime.now(tz=UTC)
    text = control_text(obs) or ""
    history = await build_history(conn, obs)
    recent = await recent_observations(conn, obs.user_id, settings, since=now - timedelta(hours=RECENT_HOURS))
    try:
        hits = await memory_repo.search(conn, obs.user_id, await embedder.embed(text), k=5)
        memory_hits = [{"text": h["text"], "score": float(h["score"])} for h in hits]
    except Exception as e:  # noqa: BLE001
        log.warning("chat.memory_search_failed", error=str(e))
        memory_hits = []
    payload = {"message": text, "history": history, "recent_observations": recent, "memory_hits": memory_hits,
               "now_iso": now.astimezone().isoformat(), "timezone": settings.TIMEZONE}
    reply, intents = "", []
    seen_ids = {r["id"] for r in recent}
    for round_no in range(1, MAX_ROUNDS + 1):
        out = await agent.chat(payload)
        reply, intents = out["reply"], out["intents"]
        need = [i for i in intents if i.get("intent") == "NeedMore"]
        if not need or round_no == MAX_ROUNDS:
            break
        q = need[0]
        more = await recent_observations(conn, obs.user_id, settings, since=now - timedelta(days=int(q.get("since_days") or 7)),
                                         query=q.get("query"))
        fresh = [m for m in more if m["id"] not in seen_ids]
        seen_ids |= {m["id"] for m in fresh}
        payload["recent_observations"] = fresh + payload["recent_observations"]
        payload["message"] = text  # same question, more data
        log.info("chat.need_more", round=round_no, query=q.get("query"), added=len(fresh))
    return reply, [i for i in intents if i.get("intent") != "NeedMore"]


async def handle_message(conn: psycopg.AsyncConnection, obs: Observation, *, settings: Settings, agent: AgentClient,
                         notifier, embedder: Embedder, on_draft=None) -> str:
    reply, intents = await converse(conn, obs, settings=settings, agent=agent, embedder=embedder)
    if not reply:
        reply = "(no reply)"
    mid = await notifier.send_text(reply)
    await observation_repo.insert(conn, Observation(
        user_id=obs.user_id, source=obs.source, source_key=f"out:{mid}", kind="message_out",
        occurred_at=datetime.now(tz=UTC), thread_key=obs.thread_key,
        payload={"text": reply, "in_reply_to": str(obs.id), "intents": [i.get("intent") for i in intents]},
    ))
    applied = await apply_intents(conn, obs, intents, embedder=embedder, on_draft=on_draft)
    log.info("chat.replied", observation_id=str(obs.id), intents=applied)
    return reply
