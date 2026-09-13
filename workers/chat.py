"""Chat worker: control-channel plain text -> history + pre-fetched context -> AgentClient.chat() -> reply.
The agent gets data in the payload (it has no DB); if it returns a NeedMore intent, we fetch more and
call again — at most MAX_ROUNDS rounds. Side-effect intents are applied here."""
from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent.client import AgentClient
from core.config import Settings
from core.embeddings import Embedder
from core.log import get_logger
from core.models import Observation
from core.phrases import phrase
from core.repo import job_repo, memory_repo, observation_repo, user_repo
from core.routing import agent_text, control_event, control_text
from core.web import WebSearcher, make_web_searcher

log = get_logger("workers.chat")

HISTORY_TURNS = 10
RECENT_HOURS = 48
RECENT_LIMIT = 50
MAX_ROUNDS = 2            # NeedMore / FindContact rounds
MAX_DOC_ROUNDS = 4        # document/web search -> read -> answer needs more hops; each hop is one API call
STALE_AFTER = timedelta(minutes=10)   # a control message this old (backlog, restart) is not answered
BUBBLE_MARK = re.compile(r"^\s*(?:---|\*\*\*|___)\s*$", re.MULTILINE)   # the agent's "new bubble here" line
MAX_BUBBLES = 3
CONTEXT_SKIP_KINDS = ("tick", "event_snapshot")   # scheduler ticks and calendar baselines are state, not things that happened


def split_bubbles(text: str) -> list[str]:
    """A reply -> the Telegram bubbles it becomes. The agent marks a natural pause with a line holding only ---;
    at most MAX_BUBBLES (the rest is folded into the last one). No marker: one bubble. Empty parts vanish."""
    parts = [p.strip() for p in BUBBLE_MARK.split(text or "") if p and p.strip()]
    if not parts:
        return []
    if len(parts) > MAX_BUBBLES:
        parts = parts[:MAX_BUBBLES - 1] + ["\n\n".join(parts[MAX_BUBBLES - 1:])]
    return parts


def bubble_pause(text: str, max_s: float) -> float:
    """How long a person would visibly "type" the next bubble: ~0.4 s plus 1 s per 120 chars, capped."""
    return min(max_s, 0.4 + len(text) / 120.0) if max_s > 0 else 0.0


async def send_reply(notifier, text: str, settings: Settings | None = None) -> tuple[str, str]:
    """Send one agent reply as 1-3 bubbles with a typing pause between them. Returns (last message id,
    the text as sent = bubbles joined by blank lines, markers removed) — the latter is what history records."""
    send = getattr(notifier, "send_rich", None) or notifier.send_text
    typing = getattr(notifier, "typing", None)
    max_pause = float(getattr(settings, "CHAT_BUBBLE_PAUSE_S", 0) or 0) if settings is not None else 0.0
    bubbles = split_bubbles(text) or [text]
    mid = ""
    for i, part in enumerate(bubbles):
        if i:
            if typing:
                await typing()
            pause = bubble_pause(part, max_pause)
            if pause:
                await asyncio.sleep(pause)
        mid = await send(part)
    return mid, "\n\n".join(bubbles)


def _turn_text(obs: Observation) -> tuple[str, str] | None:
    if obs.kind == "message_out":
        text = str(obs.payload.get("text") or "")
        if obs.payload.get("kind") == "notification":
            text = f"[notification I sent about observation {obs.payload.get('notified_observation_id')}]\n{text}"
        return "assistant", text
    text = control_text(obs)
    if not text or text.startswith("/") or "callback_query" in obs.payload:
        return None
    return "user", agent_text(obs)


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
    for k in ("from", "to", "subject", "summary", "location", "change", "start_time", "was_start"):
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
            f"select id from observation where user_id = %s and occurred_at >= %s and source <> %s and kind <> all(%s) "
            f"and ({clauses}) order by occurred_at desc limit %s",
            (user_id, since, settings.CONTROL_SOURCE, list(CONTEXT_SKIP_KINDS), *[f"%{t}%" for t in terms], limit),
        )
        ids = [r["id"] for r in await cur.fetchall()]
        rows = [await observation_repo.get(conn, i) for i in ids]
    else:
        rows = [o for o in await observation_repo.list_since(conn, user_id, since, limit=limit * 2)
                if o.source != settings.CONTROL_SOURCE and o.kind not in CONTEXT_SKIP_KINDS][:limit]
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
            if kind == "DocumentCreate":
                from adapters.composio.documents import document_url

                if registry is None or notifier is None:
                    continue
                adapter = registry.get("composio")
                handle = await adapter.connect(obs.user_id)
                try:
                    created = await adapter.create_document(handle, str(it.get("service") or "googledocs"), str(it.get("title") or "Untitled"),
                                                            str(it.get("body") or ""), it.get("parent") or None)
                    url = document_url(str(it.get("service")), str(created.get("id") or ""), created.get("url"))
                    lang = getattr(notifier, "language", None)
                    title = it.get("title") or "Document"
                    await notifier.send_text(phrase(lang, "doc_created", title=title, url=url) if url else phrase(lang, "doc_created_nolink", title=title))
                    log.info("chat.document_created", document_id=str(created.get("id") or ""))
                    applied.append("DocumentCreate")
                except Exception as e:  # noqa: BLE001
                    log.error("chat.document_create_failed", error=str(e))
                    await notifier.send_text(phrase(getattr(notifier, "language", None), "doc_failed"))
                continue
            if kind == "CalendarWrite":
                from workers import actions

                event = {k: v for k, v in it.items() if k != "intent"}
                lang = getattr(notifier, "language", None)
                if actions.calendar_needs_confirmation(event):   # other people get told: a card, nothing before the tap
                    if on_draft is None:
                        log.info("chat.calendar_draft_ignored", op=event.get("op"))
                        continue
                    await on_draft(conn, obs, {"channel": "calendar", "subject": actions.event_title(event),
                                               "body": event.get("description") or "", "to": list(event.get("attendees") or []),
                                               "event": event})
                    applied.append("CalendarDraft")
                    continue
                if registry is None or notifier is None:
                    continue
                adapter = registry.get("composio")
                handle = await adapter.connect(obs.user_id)
                try:
                    done = await adapter.calendar_write(handle, event)
                    await notifier.send_text(actions.event_done_line(event, lang, url=str(done.get("url") or "")))
                    log.info("chat.calendar_written", op=event.get("op"), event_id=str(done.get("id") or ""))
                    applied.append("CalendarWrite")
                except Exception as e:  # noqa: BLE001
                    log.error("chat.calendar_write_failed", op=event.get("op"), error=str(e)[:300])
                    await notifier.send_text(phrase(lang, "cal_direct_failed"))
                continue
            if kind == "LearnConfirm":
                from workers import learn

                await learn.confirm(conn, obs, it, embedder=embedder)
                applied.append("LearnConfirm")
                continue
            if kind == "ProfileUpdate":
                fields = {k: (it.get(k) or "").strip() or None for k in ("profile", "language", "timezone", "display_name")}
                await user_repo.update(conn, obs.user_id, **fields)
                if fields.get("language") and notifier is not None and hasattr(notifier, "language"):
                    notifier.language = fields["language"]      # buttons and fixed texts follow the switch at once
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
         "timezone": user.get("timezone") or "UTC", "emails": user_repo.own_emails(user)}
    state = await onboarding.user_state(conn, user, registry)
    pending = learn.pending_for_state(user)
    if pending:
        state["pending_learn"] = pending
    return u, state


_web_searchers: dict[tuple[str, str], WebSearcher | None] = {}


def default_web(settings: Settings) -> WebSearcher | None:
    """One searcher per configuration for the process (it owns an HTTP client); None when the web is off."""
    key = (settings.WEB_SEARCH_ENGINE, settings.TAVILY_API_KEY)
    if key not in _web_searchers:
        _web_searchers[key] = make_web_searcher(settings.WEB_SEARCH_ENGINE, tavily_api_key=settings.TAVILY_API_KEY)
    return _web_searchers[key]


def chat_text(obs: Observation) -> str:
    """The person's turn as the agent reads it: the reply-context line (if they tapped reply) and their text,
    with a Telegram command turned into plain words."""
    from workers.commands import as_chat_text

    ev = control_event(obs)
    return "\n".join(p for p in (ev.reply_to, as_chat_text(ev.text)) if p)


async def resolve_calendar(queries: list[dict[str, Any]], store: dict[str, dict], user_id: UUID, registry, tz: str) -> None:
    """CalendarQuery intents -> results in the payload (events or free/busy), under the keys the tools look up.
    A failure is stored as an error the agent can say out loud, never as an empty (falsely free) calendar."""
    adapter = handle = None
    if registry is not None:
        try:
            adapter = registry.get("composio")
            handle = await adapter.connect(user_id)
        except Exception as e:  # noqa: BLE001
            log.warning("chat.calendar_unavailable", error=str(e)[:200])
            adapter = None
    for q in queries:
        op, key = str(q.get("op") or "events"), str(q.get("key") or "")
        bucket = "free" if op == "free" else "events"
        if adapter is None:
            store[bucket][key] = {"error": "the calendar cannot be read right now"}
            continue
        try:
            if op == "free":
                store["free"][key] = await adapter.calendar_free(handle, str(q.get("start")), str(q.get("end")), tz)
            else:
                store["events"][key] = await adapter.calendar_events(handle, str(q.get("start")), str(q.get("end")),
                                                                     str(q.get("query") or ""), tz=tz)
        except Exception as e:  # noqa: BLE001
            log.warning("chat.calendar_query_failed", op=op, error=str(e)[:200])
            store[bucket][key] = {"error": "the calendar could not be read just now"}


async def converse(conn: psycopg.AsyncConnection, obs: Observation, *, settings: Settings, agent: AgentClient,
                   embedder: Embedder, now: datetime | None = None, contact_search=None, registry=None,
                   user: dict | None = None, state: dict | None = None, mode: str | None = None,
                   first_reflex: str = "", web: WebSearcher | None | bool = False) -> tuple[str, list[dict[str, Any]]]:
    """Runs the (bounded) agent rounds; returns (reply, intents without NeedMore). `web` False = the configured
    searcher, None = no web this turn."""
    now = now or datetime.now(tz=UTC)
    if web is False:
        web = default_web(settings)

    text = chat_text(obs)
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
    if first_reflex:
        payload["first_reflex"] = first_reflex
    reply, intents = "", []
    seen_ids = {r["id"] for r in recent}
    payload["contacts"] = []
    payload["documents"] = {"search": {}, "read": {}}
    payload["web"] = {"enabled": web is not None, "search": {}, "open": {}}
    payload["calendar"] = {"events": {}, "free": {}}
    data_rounds = 0
    for round_no in range(1, MAX_DOC_ROUNDS + 1):
        out = await agent.chat(payload)
        reply, intents = out["reply"], out["intents"]
        lookups = [i for i in intents if i.get("intent") == "FindContact"]
        need = [i for i in intents if i.get("intent") == "NeedMore"]
        docq = [i for i in intents if i.get("intent") == "DocumentQuery"]
        webq = [i for i in intents if i.get("intent") == "WebQuery"]
        calq = [i for i in intents if i.get("intent") == "CalendarQuery"]
        if not need and not lookups and not docq and not webq and not calq:
            break
        if (need or lookups) and data_rounds >= MAX_ROUNDS - 1:
            break                       # NeedMore/FindContact stay bounded to MAX_ROUNDS agent calls
        if (docq or webq or calq) and not need and not lookups and round_no == MAX_DOC_ROUNDS:
            break
        data_rounds += 1
        if calq:
            await resolve_calendar(calq, payload["calendar"], obs.user_id, registry, tz)
            log.info("chat.calendar_query", round=round_no, count=len(calq))
            if not need and not lookups and not docq and not webq:
                continue
        if webq and web is not None:
            for q in webq:
                op, key = str(q.get("op") or "search"), str(q.get("key") or "")
                try:
                    if op == "open":
                        payload["web"]["open"][key] = await web.open(str(q.get("url") or ""))
                    else:
                        payload["web"]["search"][key] = await web.search(str(q.get("query") or ""))
                except Exception as e:  # noqa: BLE001 - the agent is told, and answers without the page
                    log.warning("chat.web_query_failed", op=op, error=str(e)[:200])
                    payload["web"][op if op == "open" else "search"][key] = {"error": str(e)[:200]} if op == "open" else []
            log.info("chat.web_query", round=round_no, count=len(webq))
            if not need and not lookups and not docq:
                continue
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
    return reply, [i for i in intents if i.get("intent") not in ("NeedMore", "FindContact", "DocumentQuery", "WebQuery", "CalendarQuery")]


async def already_answered(conn: psycopg.AsyncConnection, obs: Observation, *, final: bool = True) -> bool:
    """True if a reply exists for this message (excluding a stage-1 'let me check' ack when final=True)."""
    cur = await conn.execute(
        "select 1 from observation where user_id = %s and kind = 'message_out' and payload->>'in_reply_to' = %s "
        + ("and coalesce(payload->>'stage','answer') <> 'ack' " if final else "") + "limit 1",
        (obs.user_id, str(obs.id)),
    )
    return await cur.fetchone() is not None


async def collect_burst(conn: psycopg.AsyncConnection, obs: Observation, settings: Settings) -> tuple[str, list[Observation]]:
    """People type in bursts ("he" / "y", or a question split over three lines). Wait a short debounce after the
    message arrived, then absorb newer unprocessed plain messages from the same thread into this turn.
    Returns (merged text, absorbed observations)."""
    from workers.commands import is_remind

    debounce = float(getattr(settings, "CHAT_DEBOUNCE_S", 0) or 0)
    received = obs.received_at or obs.occurred_at
    wait = debounce - (datetime.now(tz=UTC) - received).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    parts = [chat_text(obs)]
    absorbed: list[Observation] = []
    if debounce > 0:
        for extra in await observation_repo.claim_followups(conn, obs):
            ev = control_event(extra)
            if ev.callback is not None or not ev.text or is_remind(ev.text):
                # not a plain message: give it back to the queue untouched
                await conn.execute("update observation set status = 'new', claimed_at = null, attempts = attempts - 1 where id = %s", (extra.id,))
                continue
            parts.append(chat_text(extra))
            absorbed.append(extra)
    if absorbed:
        log.info("chat.burst_merged", observation_id=str(obs.id), absorbed=len(absorbed))
    return "\n".join(p for p in parts if p), absorbed


async def handle_message(conn: psycopg.AsyncConnection, obs: Observation, *, settings: Settings, agent: AgentClient,
                         notifier, embedder: Embedder, on_draft=None, now: datetime | None = None, contact_search=None,
                         registry=None, web: WebSearcher | None | bool = False) -> str:
    now = now or datetime.now(tz=UTC)
    received = obs.received_at or obs.occurred_at
    if now - received > STALE_AFTER:
        log.info("chat.stale_skipped", observation_id=str(obs.id), age_s=int((now - received).total_seconds()))
        return ""
    if await already_answered(conn, obs, final=True):
        log.info("chat.already_answered", observation_id=str(obs.id))
        return ""
    acked_before = await already_answered(conn, obs, final=False)   # a retry after a failure: never re-send the reflex
    typing = getattr(notifier, "typing", None)
    if typing:
        await typing()
    import time as _time

    t_start = _time.perf_counter()
    text, absorbed = await collect_burst(conn, obs, settings)
    if absorbed:   # the merged turn is what the agent sees and what history records
        obs = obs.model_copy(update={"payload": {**obs.payload, "control": {"text": text}}})

    user, state = await user_payload(conn, obs, registry)
    # The full agent must run (no fast reflex) when the reply may need a tool: first conversation (greet + connect),
    # or an unconfirmed learn proposal is waiting (the person's "evet doğru" must reach confirm_learned).
    onboarding_needed = bool(state.get("is_new") or state.get("pending_learn"))

    # Stage 1 — first reflex (fast model): a natural "let me check…" or, for simple things, the answer itself.
    # Skipped while onboarding: the full agent must run so it can greet, ask and connect.
    ack = None
    if not onboarding_needed and not acked_before:
        try:
            history = await build_history(conn, obs, turns=4)
            ack = await agent.chat_ack({"message": text, "history": history, "now_iso": now.astimezone().isoformat(),
                                        "user": user, "user_state": state})
        except Exception as e:  # noqa: BLE001 - the ack is a nicety; the real answer must still come
            log.warning("chat.ack_failed", error=str(e))
    first_reflex = ""
    t_ack = _time.perf_counter()
    if ack and ack["message"]:
        first_reflex = ack["message"]
        mid, _ = await send_reply(notifier, ack["message"], settings)
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

    # Stage 2 — the real work (tools, memory, intents). A model failure here is answered with a short apology
    # instead of raising: the queue must not retry a conversation turn (the person would see it twice).
    try:
        reply, intents = await converse(conn, obs, settings=settings, agent=agent, embedder=embedder, now=now,
                                        contact_search=contact_search, registry=registry, user=user, state=state,
                                        first_reflex=first_reflex, web=web)
    except Exception as e:  # noqa: BLE001
        log.error("chat.converse_failed", observation_id=str(obs.id), error=str(e)[:300])
        reply, intents = phrase((user or {}).get("language"), "stuck"), []
    if state.get("is_new"):
        await user_repo.merge_state(conn, obs.user_id, {"greeted": True})
    if not reply:
        reply = "(no reply)"
    mid, reply = await send_reply(notifier, reply, settings)
    await observation_repo.insert(conn, Observation(
        user_id=obs.user_id, source=obs.source, source_key=f"out:{mid}", kind="message_out",
        occurred_at=datetime.now(tz=UTC), thread_key=obs.thread_key,
        payload={"text": reply, "in_reply_to": str(obs.id), "intents": [i.get("intent") for i in intents]},
    ))
    t_reply = _time.perf_counter()
    applied = await apply_intents(conn, obs, intents, embedder=embedder, on_draft=on_draft, registry=registry, notifier=notifier)
    log.info("chat.replied", observation_id=str(obs.id), intents=applied, ack_ms=int((t_ack - t_start) * 1000),
             answer_ms=int((t_reply - t_ack) * 1000), intents_ms=int((_time.perf_counter() - t_reply) * 1000))
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
    mid, text = await send_reply(notifier, text, settings)
    await observation_repo.insert(conn, Observation(
        user_id=user_id, source=user_row["control_source"], source_key=f"out:{mid}", kind="message_out",
        occurred_at=datetime.now(tz=UTC), thread_key=thread_key, payload={"text": text, "kind": "event_reaction", "event": event_text}))
    if intents:
        await apply_intents(conn, pseudo, intents, embedder=embedder, registry=registry, notifier=notifier)
    return text
