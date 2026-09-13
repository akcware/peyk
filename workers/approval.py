"""Telegram side of the approval flow. Glue between chat intents / callbacks and workers/actions.py.

- ActionDraft intent  -> create action -> present -> Telegram message with [Send] [Edit] [Cancel]
- act:approve         -> approve(hash) -> send via the channel's adapter -> edit the Telegram message
- act:edit            -> pending_edit for the thread; the user's next plain message (or 'edit: ...') becomes the new body
- act:reject          -> rejected
"""
from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg

from core.adapter import AdapterRegistry
from core.log import get_logger
from core.models import Observation
from core.phrases import phrase
from core.repo import action_repo, observation_repo
from core.routing import callback_data, control_text
from workers import actions

log = get_logger("workers.approval")

EDIT_PREFIX = re.compile(r"^(edit|düzelt|duzelt)\s*:\s*", re.IGNORECASE)
NOT_MODIFIED = "message is not modified"   # Telegram's answer to editing a message into what it already says


def send_failure_key(error: BaseException, *, calendar: bool = False) -> str:
    """Which fixed text explains a failed send. The raw error is logged (action.send_failed), never shown."""
    s = str(error).lower()
    if calendar:
        if "expired" in s or "401" in s or "unauthorized" in s or "invalid_grant" in s:
            return "cal_failed_auth"
        if "404" in s or "410" in s or "not found" in s or "deleted" in s:
            return "cal_failed_missing"
        return "cal_failed"
    if "thread" in s and ("404" in s or "not found" in s or "cannot access" in s):
        return "send_failed_thread"
    if "expired" in s or "401" in s or "unauthorized" in s or "invalid_grant" in s:
        return "send_failed_auth"
    return "send_failed"


class ApprovalFlow:
    def __init__(self, registry: AdapterRegistry | None, notifier: Any) -> None:
        self.registry = registry
        self.notifier = notifier
        self._connections: dict[tuple[str, str], Any] = {}   # (adapter id, user id) -> handle

    def with_notifier(self, notifier: Any) -> ApprovalFlow:
        """This flow talking to one person. Consumers run in parallel, so the shared flow must never hold anyone's
        notifier: a card once went to whoever's observation had started last, i.e. into another person's chat."""
        view = copy.copy(self)          # shares the registry and the per-user connection cache
        view.notifier = notifier
        return view

    async def _conn_for(self, channel: str, user_id: UUID):
        if self.registry is None:
            raise KeyError("no adapter registry")
        adapter = self.registry.for_channel(channel)
        # Per user, never per adapter: the handle carries that user's Composio entity, i.e. WHOSE mailbox sends.
        # One shared entry would make everyone's mail go out through whoever pressed Send first after a restart.
        key = (adapter.id, str(user_id))
        if key not in self._connections:
            self._connections[key] = await adapter.connect(user_id)
        return adapter, self._connections[key]

    @property
    def lang(self) -> str | None:
        return getattr(self.notifier, "language", None)

    async def _show(self, conn, action: dict) -> None:
        mid = await self.notifier.send_markup(actions.render_draft(action, self.lang), actions.buttons(action, self.lang))
        log.info("approval.presented", action_id=str(action["id"]), tg_message_id=mid)

    # ---- entry points ----

    async def on_draft(self, conn: psycopg.AsyncConnection, obs: Observation, intent: dict[str, Any]) -> dict:
        content = {"body": intent.get("body", ""), "subject": intent.get("subject"), "to": list(intent.get("to") or [])}
        if intent.get("event"):   # a calendar change: the card and the send read everything from here
            content["event"] = intent["event"]
        action = await action_repo.create(conn, obs.user_id, channel=intent.get("channel") or "gmail",
                                          thread_key=intent.get("thread_key") or None, content=content)
        action = await actions.present(conn, action["id"])
        await self._show(conn, action)
        return action

    async def on_callback(self, conn: psycopg.AsyncConnection, obs: Observation) -> str | None:
        parsed = actions.parse_callback(callback_data(obs))
        if parsed is None:
            return None
        verb, action_id, h = parsed
        cq = obs.payload.get("callback_query") or {}
        tg_mid = (cq.get("message") or {}).get("message_id")
        owner = await action_repo.get(conn, action_id)
        if owner is None or owner["user_id"] != obs.user_id:   # a card that reached the wrong chat does nothing
            log.warning("approval.foreign_card", action_id=str(action_id), user_id=str(obs.user_id))
            return phrase(self.lang, "card_not_yours")
        if verb == "approve":
            try:
                action = await actions.approve(conn, action_id, h)
            except actions.ApprovalMismatch:
                current = await action_repo.get(conn, action_id)
                if current and current["status"] in ("awaiting_approval", "failed"):
                    await self._show(conn, current)
                return phrase(self.lang, "draft_changed")
            except actions.InvalidTransition as e:
                return str(e)
            is_event = bool((action.get("content") or {}).get("event"))
            try:
                adapter, handle = await self._conn_for(action["channel"], obs.user_id)
                sent = await actions.send(conn, action_id, adapter, handle)
            except Exception as e:  # noqa: BLE001
                await self._edit(tg_mid, "⚠️ " + phrase(self.lang, send_failure_key(e, calendar=is_event)), actions.buttons(action, self.lang))
                return phrase(self.lang, "ack_send_failed")
            # Replace the card with what went out: the text stays visible, the id does not (it is logged).
            log.info("approval.sent", action_id=str(action_id), external_id=sent["external_id"])
            done = actions.render_done(sent, self.lang)
            await self._edit(tg_mid, done, None)
            await self._remember(conn, obs, action_id, "sent", done)
            return phrase(self.lang, "ack_cal_done" if is_event else "ack_sent")
        if verb == "edit":
            action = await action_repo.get(conn, action_id)
            if action is None or action["status"] not in ("awaiting_approval", "failed"):
                return phrase(self.lang, "nothing_to_edit")
            await action_repo.set_pending_edit(conn, obs.user_id, obs.thread_key or "", action_id)
            await self.notifier.send_text(phrase(self.lang, "edit_prompt"))
            return phrase(self.lang, "ack_waiting_edit")
        if verb == "reject":
            try:
                rejected = await actions.reject(conn, action_id)
            except actions.InvalidTransition as e:
                return str(e)
            is_event = bool(((rejected or {}).get("content") or {}).get("event"))
            text = phrase(self.lang, "cal_cancelled" if is_event else "cancelled")
            await self._edit(tg_mid, text, None)
            await self._remember(conn, obs, action_id, "cancelled", text)
            return phrase(self.lang, "ack_cancelled")
        return None

    async def maybe_apply_edit(self, conn: psycopg.AsyncConnection, obs: Observation) -> bool:
        """If the thread is in edit mode (or the text has an edit: prefix), apply the text as the new body."""
        text = control_text(obs) or ""
        pending = await action_repo.get_pending_edit(conn, obs.user_id, obs.thread_key or "")
        m = EDIT_PREFIX.match(text)
        if m:
            text = text[m.end():]
        elif pending is None:
            return False
        if pending is None:
            # calendar cards have no Edit: they change by telling Peyk, so "edit:" never rewrites one
            open_actions = [a for a in await action_repo.list_open(conn, obs.user_id) if not (a["content"] or {}).get("event")]
            if not open_actions:
                await self.notifier.send_text(phrase(self.lang, "no_draft"))
                return True
            pending = open_actions[0]["id"]
        action = await action_repo.get(conn, pending)
        new_content = {**action["content"], "body": text.strip()}
        try:
            action = await actions.edit(conn, pending, new_content)
        except ValueError as e:
            await self.notifier.send_text(str(e))
            return True
        await action_repo.set_pending_edit(conn, obs.user_id, obs.thread_key or "", None)
        await self._show(conn, action)
        return True

    async def _remember(self, conn, obs: Observation, action_id, status: str, text: str) -> None:
        """What a button did becomes part of the conversation. The card is only edited in Telegram, so the chat agent
        could not tell that an invitation had already gone out and prepared it a second time."""
        await observation_repo.insert(conn, Observation(
            user_id=obs.user_id, source=obs.source, source_key=f"act:{action_id}:{status}", kind="message_out",
            occurred_at=datetime.now(tz=UTC), thread_key=obs.thread_key,
            payload={"text": text, "kind": "action_result", "action_id": str(action_id), "status": status}))

    async def _edit(self, tg_message_id: int | None, text: str, markup: dict | None) -> None:
        if tg_message_id is None:
            await self.notifier.send_text(text)
            return
        try:
            await self.notifier.edit_message(tg_message_id, text, markup)
        except Exception as e:  # noqa: BLE001
            if NOT_MODIFIED in str(e).lower():
                return   # the card already says this (a repeated Send on the same failure): nothing new to show
            log.warning("approval.edit_message_failed", error=str(e))
            await self.notifier.send_text(text)
