"""Live-queue consumer (phase 1): claim -> route by kind/channel -> triage -> gate -> notify -> complete.

No per-source branches here. Routing is by data: control-channel observations (the user's own Telegram)
are commands/callbacks/chat; everything else is a world event that goes through triage + gate.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from agent.client import AgentClient
from agent.schemas import TriageResult
from core import db, queue
from core.adapter import SourceAdapter
from core.config import Settings
from core.identity import display_name_from_header, normalize_email
from core.log import get_logger
from core.models import Content, Observation
from core.repo import budget_repo, identity_repo, observation_repo
from core.routing import is_callback, is_control_channel
from workers import feedback, gate, gate_state

log = get_logger("workers.triage")


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
        p = obs.payload
        urgency = "‼️" if triage.urgency >= 5 else "❗" if triage.urgency == 4 else "•"
        who = p.get("from") or p.get("summary") or obs.source
        subject = p.get("subject") or p.get("summary") or "(no subject)"
        snippet = (p.get("snippet") or p.get("text") or "").strip()
        body = f"{urgency} [{obs.source}] {who}\n{subject}"
        if snippet:
            body += f"\n\n{snippet[:280]}"
        body += f"\n\n_{triage.reason}_"
        return body

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
        mid = await self.adapter.send(handle, self.chat_id, Content(text=self.render(obs, triage), reply_markup=self.buttons(sent_id)))
        await conn.execute("update sent_notification set tg_message_id = %s where id = %s", (int(mid), sent_id))
        return sent_id

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
    observation = {"source": obs.source, "kind": obs.kind, "occurred_at": obs.occurred_at.isoformat(), "payload": obs.payload}
    result, meta = await agent.triage(observation, ctx)
    await budget_repo.insert_triage(conn, obs.id, urgency=result.urgency, category=result.category, reason=result.reason,
                                    model_id=meta.get("model_id", "?"), latency_ms=meta.get("latency_ms"))
    state = await gate_state.load(conn, obs, now)
    decision = gate.decide(obs, result, state)
    log.info("triage.decided", observation_id=str(obs.id), urgency=result.urgency, category=result.category,
             notify=decision.notify, reason=decision.reason, latency_ms=meta.get("latency_ms"))
    if decision.notify:
        await notifier.send(conn, obs, result)
    return decision


async def handle(obs: Observation, *, settings: Settings, agent: AgentClient, notifier: Notifier, now: datetime | None = None) -> None:
    now = now or datetime.now(tz=UTC)
    async with db.connection() as conn:
        if is_control_channel(obs, settings):
            if is_callback(obs):
                text = await feedback.apply(conn, obs)
                await notifier.ack(obs, text)
            # plain control-channel messages: commands (phase 2) / chat (phase 3)
            return
        if obs.kind == "message_in":
            await triage_and_gate(conn, obs, agent=agent, notifier=notifier, now=now)
            return
        log.info("triage.skipped_kind", kind=obs.kind, observation_id=str(obs.id))


async def run(settings: Settings, *, agent: AgentClient, notifier: Notifier, idle_sleep: float = 1.0) -> None:
    user_id = settings.USER_ID
    while True:
        async with db.connection() as conn:
            obs = await queue.claim_next(conn, user_id)
        if obs is None:
            await asyncio.sleep(idle_sleep)
            continue
        slog = log.bind(observation_id=str(obs.id), source=obs.source, kind=obs.kind)
        try:
            await handle(obs, settings=settings, agent=agent, notifier=notifier)
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
