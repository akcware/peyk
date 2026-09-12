"""Chat worker: control-channel plain text -> history + pre-fetched context -> AgentClient.chat() -> reply.
The agent gets data in the payload (it has no DB); if it returns a NeedMore intent, we fetch more and
call again — at most MAX_ROUNDS rounds. Side-effect intents are applied here."""
from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent.client import AgentClient
from core.config import Settings
from core.embeddings import Embedder
from core.log import get_logger
from core.models import Observation
from core.repo import job_repo, memory_repo, observation_repo, user_repo
from core.routing import control_text

log = get_logger("workers.chat")

HISTORY_TURNS = 10
RECENT_HOURS = 48
RECENT_LIMIT = 50
MAX_ROUNDS = 2            # NeedMore / FindContact rounds
MAX_DOC_ROUNDS = 4        # document search -> read -> answer needs more hops; each hop is one API call
STALE_AFTER = timedelta(minutes=10)   # a control message this old (backlog, restart) is not answered


def _turn_text(obs: Observation) -> tuple[str, str] | None:
    if obs.kind == "message_out":
        text = str(obs.payload.get("text") or "")
        if obs.payload.get("kind") == "notification":
            text = f"[notification I sent about observation {obs.payload.get('notified_observation_id')}]\n{text}"
        return "assistant", text
    text = control_text(obs)
    if not text or text.startswith("/") or "callback_query" in obs.payload:
        return None
    return "user", text


async def build_history(conn: psycopg.AsyncConnection, obs: Observation, *, turns: int = HISTORY_TURNS) -> list[dict[str, str]]:
    rows = await observation_repo.list_by_thread(conn, obs.user_id, obs.thread_key or "", limit=turns * 3 + 5)
    out: list[dict[str, str]] = []
    for r in rows:                     # newest first
        if r.id == obs.id or r.occurred_at >= obs.occurred_at:
            continue                   # never include this message or anything that arrived after it
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
        d["snippet"] = html.unescape(str(snippet))[:240]
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
    cur = await conn.execute("select observation_id, urgency, summary from triage where observation_id = any(%s)", ([o.id for o in rows],))
    tri = {r["observation_id"]: r for r in await cur.fetchall()}
    cur = await conn.execute("select observation_id, sent_at from sent_notification where observation_id = any(%s)", ([o.id for o in rows],))
    notified = {r["observation_id"]: r["sent_at"] for r in await cur.fetchall()}
    out = []
    for o in rows:
        d = compact(o, tri[o.id]["urgency"] if o.id in tri else None)
        if o.id in tri and tri[o.id]["summary"]:
            d["summary"] = tri[o.id]["summary"]
        if o.id in notified:
            d["notified_at"] = notified[o.id].isoformat()
        out.append(d)
    return out


async def apply_intents(conn: psycopg.AsyncConnection, obs: Observation, intents: list[dict[str, Any]], *,
                        embedder: Embedder, on_draft=None, registry=None, notifier=None) -> list[str]:
    applied: list[str] = []
    for it in intents:
        kind = it.get("intent")
        try:
            if kind == "ConnectRequest":
                from workers import onboarding

                user = await user_repo.get(conn, obs.user_id)
                if user is not None and notifier is not None:
                    await notifier.send_text(await onboarding.start_connection(conn, user, str(it.get("service") or ""), registry))
                    applied.append("ConnectRequest")
                continue
            if kind == "DeleteAccountRequest":
                from workers import account

                user = await user_repo.get(conn, obs.user_id)
                if user is not None and notifier is not None:
                    await account.start(conn, user, notifier)
                    applied.append("DeleteAccountRequest")
                continue
            if kind == "LearnConfirm":
                from workers import learn

                await learn.confirm(conn, obs, it, embedder=embedder)
                applied.append("LearnConfirm")
                continue
            if kind == "ProfileUpdate":
                fields = {k: (it.get(k) or "").strip() or None for k in ("profile", "language", "timezone", "display_name")}
                await user_repo.update(conn, obs.user_id, **fields)
                applied.append("ProfileUpdate")
                continue
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


async def user_payload(conn: psycopg.AsyncConnection, obs: Observation, registry=None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(user dict for the agent, user_state for onboarding). Falls back to env-configured single user."""
    from workers import onboarding

    user = await user_repo.get(conn, obs.user_id)
    if user is None:
        return {}, {}
    from workers import learn

    u = {"display_name": user.get("display_name"), "profile": user.get("profile") or "", "language": user.get("language") or "en",
         "timezone": user.get("timezone") or "UTC"}
    state = await onboarding.user_state(conn, user, registry)
    pending = learn.pending_for_state(user)
    if pending:
        state["pending_learn"] = pending
    return u, state


async def converse(conn: psycopg.AsyncConnection, obs: Observation, *, settings: Settings, agent: AgentClient,
                   embedder: Embedder, now: datetime | None = None, contact_search=None, registry=None,
                   user: dict | None = None, state: dict | None = None, mode: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Runs the (bounded) agent rounds; returns (reply, intents without NeedMore)."""
    now = now or datetime.now(tz=UTC)
    from workers.commands import as_chat_text

    text = as_chat_text(control_text(obs))
    if user is None:
        user, state = await user_payload(conn, obs, registry)
    history = await build_history(conn, obs)
    recent = await recent_observations(conn, obs.user_id, settings, since=now - timedelta(hours=RECENT_HOURS))
    try:
        hits = await memory_repo.search(conn, obs.user_id, await embedder.embed(text), k=5)
        memory_hits = [{"text": h["text"], "score": float(h["score"])} for h in hits]
    except Exception as e:  # noqa: BLE001
        log.warning("chat.memory_search_failed", error=str(e))
        memory_hits = []
    tz = (user or {}).get("timezone") or settings.TIMEZONE
    try:
        from zoneinfo import ZoneInfo

        now_local = now.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        now_local = now.astimezone()
    payload = {"message": text, "history": history, "recent_observations": recent, "memory_hits": memory_hits,
               "now_iso": now_local.isoformat(), "timezone": tz, "user": user or {}, "user_state": state or {}}
    if mode:
        payload["mode"] = mode
    reply, intents = "", []
    seen_ids = {r["id"] for r in recent}
    payload["contacts"] = []
    payload["documents"] = {"search": {}, "read": {}}
    data_rounds = 0
    for round_no in range(1, MAX_DOC_ROUNDS + 1):
        out = await agent.chat(payload)
        reply, intents = out["reply"], out["intents"]
        lookups = [i for i in intents if i.get("intent") == "FindContact"]
        need = [i for i in intents if i.get("intent") == "NeedMore"]
        docq = [i for i in intents if i.get("intent") == "DocumentQuery"]
        if not need and not lookups and not docq:
            break
        if (need or lookups) and data_rounds >= MAX_ROUNDS - 1:
            break                       # NeedMore/FindContact stay bounded to MAX_ROUNDS agent calls
        if docq and not need and not lookups and round_no == MAX_DOC_ROUNDS:
            break
        data_rounds += 1
        if docq and registry is not None:
            try:
                adapter = registry.get("composio")
                handle = await adapter.connect(obs.user_id)
                for q in docq:
                    if q.get("op") == "search":
                        res = await adapter.search_documents(handle, str(q.get("query") or ""), [q["service"]] if q.get("service") else None)
                        payload["documents"]["search"][q["key"]] = res
                    elif q.get("op") == "read":
                        payload["documents"]["read"][q["key"]] = await adapter.read_document(handle, str(q.get("service")), str(q.get("id")))
                log.info("chat.document_query", round=round_no, count=len(docq))
            except Exception as e:  # noqa: BLE001
                log.warning("chat.document_query_failed", error=str(e))
            if not need and not lookups:
                continue
        if lookups:
            names = {str(i.get("name") or "").strip() for i in lookups if i.get("name")}
            found: list[dict] = []
            for name in names:
                found += await contact_search(conn, name) if contact_search else []
            payload["contacts"] = found + payload["contacts"]
            log.info("chat.find_contact", round=round_no, names=sorted(names), found=len(found))
            if not need:
                continue
        q = need[0]
        more = await recent_observations(conn, obs.user_id, settings, since=now - timedelta(days=int(q.get("since_days") or 7)),
                                         query=q.get("query"))
        fresh = [m for m in more if m["id"] not in seen_ids]
        seen_ids |= {m["id"] for m in fresh}
        payload["recent_observations"] = fresh + payload["recent_observations"]
        payload["message"] = text  # same question, more data
        log.info("chat.need_more", round=round_no, query=q.get("query"), added=len(fresh))
    return reply, [i for i in intents if i.get("intent") not in ("NeedMore", "FindContact", "DocumentQuery")]


async def already_answered(conn: psycopg.AsyncConnection, obs: Observation, *, final: bool = True) -> bool:
    """True if a reply exists for this message (excluding a stage-1 'let me check' ack when final=True)."""
    cur = await conn.execute(
        "select 1 from observation where user_id = %s and kind = 'message_out' and payload->>'in_reply_to' = %s "
        + ("and coalesce(payload->>'stage','answer') <> 'ack' " if final else "") + "limit 1",
        (obs.user_id, str(obs.id)),
    )
    return await cur.fetchone() is not None


async def handle_message(conn: psycopg.AsyncConnection, obs: Observation, *, settings: Settings, agent: AgentClient,
                         notifier, embedder: Embedder, on_draft=None, now: datetime | None = None, contact_search=None,
                         registry=None) -> str:
    now = now or datetime.now(tz=UTC)
    received = obs.received_at or obs.occurred_at
    if now - received > STALE_AFTER:
        log.info("chat.stale_skipped", observation_id=str(obs.id), age_s=int((now - received).total_seconds()))
        return ""
    if await already_answered(conn, obs, final=True):
        log.info("chat.already_answered", observation_id=str(obs.id))
        return ""
    typing = getattr(notifier, "typing", None)
    if typing:
        await typing()
    send = getattr(notifier, "send_rich", None) or notifier.send_text
    from workers.commands import as_chat_text

    text = as_chat_text(control_text(obs))

    user, state = await user_payload(conn, obs, registry)
    onboarding_needed = bool(state.get("is_new"))   # first conversation: the full agent greets and offers connections

    # Stage 1 — first reflex (fast model): a natural "let me check…" or, for simple things, the answer itself.
    # Skipped while onboarding: the full agent must run so it can greet, ask and connect.
    ack = None
    if not onboarding_needed:
        try:
            history = await build_history(conn, obs, turns=4)
            ack = await agent.chat_ack({"message": text, "history": history, "now_iso": now.astimezone().isoformat(),
                                        "user": user, "user_state": state})
        except Exception as e:  # noqa: BLE001 - the ack is a nicety; the real answer must still come
            log.warning("chat.ack_failed", error=str(e))
    if ack and ack["message"]:
        mid = await send(ack["message"])
        await observation_repo.insert(conn, Observation(
            user_id=obs.user_id, source=obs.source, source_key=f"out:{mid}", kind="message_out",
            occurred_at=datetime.now(tz=UTC), thread_key=obs.thread_key,
            payload={"text": ack["message"], "in_reply_to": str(obs.id), "stage": "ack" if ack["needs_work"] else "answer"},
        ))
        if not ack["needs_work"]:
            log.info("chat.answered_directly", observation_id=str(obs.id))
            return ack["message"]
        if typing:
            await typing()

    # Stage 2 — the real work (tools, memory, intents).
    reply, intents = await converse(conn, obs, settings=settings, agent=agent, embedder=embedder, now=now,
                                    contact_search=contact_search, registry=registry, user=user, state=state)
    if state.get("is_new"):
        await user_repo.merge_state(conn, obs.user_id, {"greeted": True})
    if not reply:
        reply = "(no reply)"
    mid = await send(reply)
    await observation_repo.insert(conn, Observation(
        user_id=obs.user_id, source=obs.source, source_key=f"out:{mid}", kind="message_out",
        occurred_at=datetime.now(tz=UTC), thread_key=obs.thread_key,
        payload={"text": reply, "in_reply_to": str(obs.id), "intents": [i.get("intent") for i in intents]},
    ))
    applied = await apply_intents(conn, obs, intents, embedder=embedder, on_draft=on_draft, registry=registry, notifier=notifier)
    log.info("chat.replied", observation_id=str(obs.id), intents=applied)
    return reply


async def react_to_event(conn: psycopg.AsyncConnection, user_id: UUID, event_text: str, *, settings: Settings, agent: AgentClient,
                         notifier, embedder: Embedder, registry=None, fallback: str | None = None,
                         instruction: str = "tell the person now, in one or two sentences") -> str:
    """Let the agent phrase a system event (a service got connected, a link expired) in its own voice and the
    person's language, with the conversation context. Falls back to `fallback` text if the agent fails."""
    user_row = await user_repo.get(conn, user_id)
    if user_row is None:
        return ""
    thread_key = user_row["control_thread_key"]
    pseudo = Observation(user_id=user_id, source=user_row["control_source"], source_key=f"event:{datetime.now(tz=UTC).timestamp()}",
                         kind="message_in", occurred_at=datetime.now(tz=UTC), thread_key=thread_key,
                         payload={"control": {"text": f"[system event: {event_text} — {instruction}]"}})
    try:
        user, state = await user_payload(conn, pseudo, registry)
        reply, intents = await converse(conn, pseudo, settings=settings, agent=agent, embedder=embedder, registry=registry,
                                        user=user, state=state, mode="event")
    except Exception as e:  # noqa: BLE001
        log.warning("chat.event_reaction_failed", error=str(e))
        reply, intents = "", []
    text = reply.strip() or (fallback or "")
    if not text:
        return ""
    send = getattr(notifier, "send_rich", None) or notifier.send_text
    mid = await send(text)
    await observation_repo.insert(conn, Observation(
        user_id=user_id, source=user_row["control_source"], source_key=f"out:{mid}", kind="message_out",
        occurred_at=datetime.now(tz=UTC), thread_key=thread_key, payload={"text": text, "kind": "event_reaction", "event": event_text}))
    if intents:
        await apply_intents(conn, pseudo, intents, embedder=embedder, registry=registry, notifier=notifier)
    return text
