"""Live-queue consumer (phase 1): claim -> route by kind/channel -> triage -> gate -> notify -> complete.

No per-source branches here. Routing is by data: control-channel observations (the user's own Telegram)
are commands/callbacks/chat; everything else is a world event that goes through triage + gate.
"""
from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from agent.client import AgentClient
from agent.schemas import TriageResult
from core import db, queue
from core.adapter import SourceAdapter
from core.config import Settings
from core.identity import display_name_from_header, normalize_email
from core.log import get_logger
from core.models import Content, Observation
from core.repo import budget_repo, identity_repo, job_repo, observation_repo, user_repo
from core.routing import callback_data, control_text, is_callback, is_control_channel
from workers import account, chat, commands, contacts, feedback, gate, gate_state, ticks

log = get_logger("workers.triage")


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_ITALIC = re.compile(r"(?<![\w*])_([^_\n]+)_(?![\w*])")


def md_to_telegram_html(text: str) -> str:
    """Minimal markdown -> Telegram HTML: **bold**, `code`, _italic_. Everything else is escaped."""
    out = html.escape(text, quote=False)
    out = _MD_BOLD.sub(r"<b>\1</b>", out)
    out = _MD_CODE.sub(r"<code>\1</code>", out)
    out = _MD_ITALIC.sub(r"<i>\1</i>", out)
    return out


def strip_md(text: str) -> str:
    return _MD_CODE.sub(r"\1", _MD_BOLD.sub(r"\1", text))


class Notifier:
    """Sends gated notifications to the control channel and records them."""

    def __init__(self, adapter: SourceAdapter, chat_id: str, user_id: UUID) -> None:
        self.adapter = adapter
        self.chat_id = chat_id
        self.user_id = user_id
        self._conn = None

    async def _connection(self):
        if self._conn is None:
            self._conn = await self.adapter.connect(self.user_id)
        return self._conn

    @staticmethod
    def render(obs: Observation, triage: TriageResult) -> str:
        """Assistant-style notification: who · source, subject, then the model's secretary summary.
        Raw text only as a fallback when there is no summary."""
        p = obs.payload
        urgency = "‼️" if triage.urgency >= 5 else "❗" if triage.urgency == 4 else "•"
        raw_from = str(p.get("from") or p.get("organizer_email") or "")
        who = display_name_from_header(raw_from) or normalize_email(raw_from) if raw_from else str(p.get("summary") or obs.source)
        subject = html.unescape(str(p.get("subject") or p.get("summary") or "")).strip()
        head = f"{urgency} {html.unescape(who)} · {obs.source}"
        if subject:
            head += f"\n{subject}"
        body = triage.summary.strip() or html.unescape(str(p.get("snippet") or p.get("text") or "")).strip()[:280] or triage.reason
        return f"{head}\n\n{body}"

    @staticmethod
    def buttons(sent_id: UUID) -> dict:
        return {"inline_keyboard": [[
            {"text": "👍 useful", "callback_data": f"fb:useful:{sent_id}"},
            {"text": "👎 noise", "callback_data": f"fb:noise:{sent_id}"},
            {"text": "🔇 mute thread", "callback_data": f"mute:thread:{sent_id}"},
        ]]}

    async def send(self, conn, obs: Observation, triage: TriageResult) -> UUID:
        sent_id = await budget_repo.insert_sent(conn, self.user_id, obs.id, thread_key=obs.thread_key, urgency=triage.urgency, tg_message_id=None)
        handle = await self._connection()
        text = self.render(obs, triage)
        mid = await self.adapter.send(handle, self.chat_id, Content(text=text, reply_markup=self.buttons(sent_id)))
        await conn.execute("update sent_notification set tg_message_id = %s where id = %s", (int(mid), sent_id))
        # The notification is something *we said* in the control thread: record it so the chat agent knows about it.
        await observation_repo.insert(conn, Observation(
            user_id=self.user_id, source=self.adapter.id, source_key=f"out:{mid}", kind="message_out",
            occurred_at=datetime.now(tz=UTC), thread_key=self.chat_id,
            payload={"text": text, "kind": "notification", "notified_observation_id": str(obs.id),
                     "notified_source": obs.source, "notified_thread_key": obs.thread_key, "urgency": triage.urgency},
        ))
        return sent_id

    async def send_text(self, text: str) -> str:
        handle = await self._connection()
        return await self.adapter.send(handle, self.chat_id, Content(text=text))

    async def send_rich(self, text: str) -> str:
        """Agent replies are markdown-ish; render as Telegram HTML, fall back to plain text on rejection."""
        handle = await self._connection()
        try:
            return await self.adapter.send(handle, self.chat_id, Content(text=md_to_telegram_html(text), extra={"parse_mode": "HTML"}))
        except Exception as e:  # noqa: BLE001 - never lose a reply over formatting
            log.warning("notify.html_rejected", error=str(e))
            return await self.adapter.send(handle, self.chat_id, Content(text=strip_md(text)))

    async def typing(self) -> None:
        action = getattr(self.adapter, "send_chat_action", None)
        if action is None:
            return
        try:
            await action(self.chat_id, "typing")
        except Exception as e:  # noqa: BLE001
            log.debug("notify.typing_failed", error=str(e))

    async def send_content(self, content: Content) -> str:
        handle = await self._connection()
        return await self.adapter.send(handle, self.chat_id, content)

    async def send_markup(self, text: str, reply_markup: dict) -> str:
        handle = await self._connection()
        return await self.adapter.send(handle, self.chat_id, Content(text=text, reply_markup=reply_markup))

    async def edit_message(self, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        edit = getattr(self.adapter, "edit_message", None)
        if edit is None:
            await self.send_text(text)
            return
        await edit(self.chat_id, message_id, text, reply_markup or {"inline_keyboard": []})

    async def ack(self, obs: Observation, text: str | None) -> None:
        cq = obs.payload.get("callback_query") or {}
        answer = getattr(self.adapter, "answer_callback", None)
        if answer and cq.get("id"):
            try:
                await answer(cq["id"], text)
            except Exception as e:  # noqa: BLE001 - acking is best effort
                log.warning("feedback.ack_failed", error=str(e))


async def sender_context(conn, obs: Observation) -> dict | None:
    sender = obs.payload.get("from")
    if not sender:
        return None
    email = normalize_email(str(sender))
    person_id = await identity_repo.resolve(conn, obs.user_id, "email", email, display_name=display_name_from_header(str(sender)))
    prior = await observation_repo.count_from_sender(conn, obs.user_id, email)
    return {"sender": email, "prior_messages_from_sender": max(prior - 1, 0), "known_person": str(person_id)}


async def triage_and_gate(conn, obs: Observation, *, agent: AgentClient, notifier: Notifier, now: datetime) -> gate.Decision:
    ctx = await sender_context(conn, obs)
    user = await user_repo.get(conn, obs.user_id) or {}
    observation = {"source": obs.source, "kind": obs.kind, "occurred_at": obs.occurred_at.isoformat(), "payload": obs.payload,
                   "user": {"profile": user.get("profile") or "", "language": user.get("language") or "", "display_name": user.get("display_name")}}
    result, meta = await agent.triage(observation, ctx)
    await budget_repo.insert_triage(conn, obs.id, urgency=result.urgency, category=result.category, reason=result.reason,
                                    summary=result.summary, model_id=meta.get("model_id", "?"), latency_ms=meta.get("latency_ms"))
    state = await gate_state.load(conn, obs, now)
    decision = gate.decide(obs, result, state)
    log.info("triage.decided", observation_id=str(obs.id), urgency=result.urgency, category=result.category,
             notify=decision.notify, reason=decision.reason, latency_ms=meta.get("latency_ms"))
    if decision.notify:
        await notifier.send(conn, obs, result)
    return decision


async def handle_command(conn, obs: Observation, *, settings: Settings, notifier: Notifier, now: datetime) -> None:
    text = control_text(obs) or ""
    if text.split()[0].split("@")[0] == "/remind":
        try:
            r = commands.parse_remind(text, now, settings.TIMEZONE)
        except ValueError as e:
            await notifier.send_text(str(e))
            return
        await job_repo.create(conn, obs.user_id, run_at=r.run_at, kind="followup", payload={"note": r.note}, created_by="user")
        local = r.run_at.astimezone(ZoneInfo(settings.TIMEZONE))
        await notifier.send_text(f"⏰ Will remind you at {local:%a %H:%M}: {r.note}")
        return
    await notifier.send_text("Commands: /remind <1h|09:30|tomorrow [09:30]> <note>")


async def handle(obs: Observation, *, settings: Settings, agent: AgentClient, notifier: Notifier | None = None,
                 now: datetime | None = None, tick_ctx: ticks.TickContext | None = None,
                 embedder=None, approval=None, notifiers=None) -> None:
    now = now or datetime.now(tz=UTC)
    async with db.connection() as conn:
        if notifier is None and notifiers is not None:
            notifier = await notifiers.for_user(conn, obs.user_id)
        if approval is not None and notifier is not None:
            approval.notifier = notifier
        if is_control_channel(obs, settings):
            if is_callback(obs):
                data = callback_data(obs) or ""
                if approval is not None and data.startswith("act:"):
                    text = await approval.on_callback(conn, obs)
                elif data.startswith("del:"):
                    text = await account.on_callback(conn, obs.user_id, data, notifier, tick_ctx.registry if tick_ctx else None)
                else:
                    text = await feedback.apply(conn, obs)
                await notifier.ack(obs, text)
            elif commands.is_remind(control_text(obs)):
                await handle_command(conn, obs, settings=settings, notifier=notifier, now=now)
            elif approval is not None and await approval.maybe_apply_edit(conn, obs):
                pass
            elif control_text(obs) and embedder is not None:
                registry = tick_ctx.registry if tick_ctx else None
                await chat.handle_message(conn, obs, settings=settings, agent=agent, notifier=notifier,
                                          embedder=embedder, on_draft=approval.on_draft if approval else None,
                                          contact_search=contacts.make_contact_search(registry, obs.user_id), registry=registry)
            return
        if obs.kind == "tick":
            ctx = tick_ctx or ticks.TickContext(settings=settings, registry=None, notifier=notifier)
            if ctx.retriage is None:
                async def _retriage(c, o):
                    return await triage_and_gate(c, o, agent=agent, notifier=notifier, now=datetime.now(tz=UTC))
                ctx.retriage = _retriage
            await ticks.handle_tick(conn, obs, ctx)
            return
        # every other observation is a world event (message_in, event_starting, ...): triage + gate
        await triage_and_gate(conn, obs, agent=agent, notifier=notifier, now=now)


async def run(settings: Settings, *, agent: AgentClient, notifier: Notifier | None = None, idle_sleep: float = 1.0,
              tick_ctx: ticks.TickContext | None = None, embedder=None, approval=None, notifiers=None) -> None:
    while True:
        async with db.connection() as conn:
            obs = await queue.claim_next(conn, None)   # all users
        if obs is None:
            await asyncio.sleep(idle_sleep)
            continue
        slog = log.bind(observation_id=str(obs.id), source=obs.source, kind=obs.kind)
        try:
            await handle(obs, settings=settings, agent=agent, notifier=notifier, tick_ctx=tick_ctx,
                         embedder=embedder, approval=approval, notifiers=notifiers)
            async with db.connection() as conn:
                await queue.complete(conn, obs.id)
            slog.info("triage.done")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            async with db.connection() as conn:
                status = await queue.fail(conn, obs.id)
            slog.error("triage.failed", error=str(e), status=status, attempts=obs.attempts)
            await asyncio.sleep(min(2 ** obs.attempts, 30))
