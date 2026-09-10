"""Telegram side of the approval flow. Glue between chat intents / callbacks and workers/actions.py.

- ActionDraft intent  -> create action -> present -> Telegram message with [Send] [Edit] [Cancel]
- act:approve         -> approve(hash) -> send via the channel's adapter -> edit the Telegram message
- act:edit            -> pending_edit for the thread; the user's next plain message (or 'edit: ...') becomes the new body
- act:reject          -> rejected
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import psycopg

from core.adapter import AdapterRegistry
from core.log import get_logger
from core.models import Observation
from core.repo import action_repo
from core.routing import callback_data, control_text
from workers import actions

log = get_logger("workers.approval")

EDIT_PREFIX = re.compile(r"^(edit|düzelt|duzelt)\s*:\s*", re.IGNORECASE)


class ApprovalFlow:
    def __init__(self, registry: AdapterRegistry | None, notifier: Any) -> None:
        self.registry = registry
        self.notifier = notifier
        self._connections: dict[str, Any] = {}

    async def _conn_for(self, channel: str, user_id: UUID):
        if self.registry is None:
            raise KeyError("no adapter registry")
        adapter = self.registry.for_channel(channel)
        if adapter.id not in self._connections:
            self._connections[adapter.id] = await adapter.connect(user_id)
        return adapter, self._connections[adapter.id]

    async def _show(self, conn, action: dict) -> None:
        mid = await self.notifier.send_markup(actions.render_draft(action), actions.buttons(action))
        log.info("approval.presented", action_id=str(action["id"]), tg_message_id=mid)

    # ---- entry points ----

    async def on_draft(self, conn: psycopg.AsyncConnection, obs: Observation, intent: dict[str, Any]) -> dict:
        content = {"body": intent.get("body", ""), "subject": intent.get("subject"), "to": list(intent.get("to") or [])}
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
        if verb == "approve":
            try:
                action = await actions.approve(conn, action_id, h)
            except actions.ApprovalMismatch:
                current = await action_repo.get(conn, action_id)
                if current and current["status"] in ("awaiting_approval", "failed"):
                    await self._show(conn, current)
                return "Draft changed — approve the newest version"
            except actions.InvalidTransition as e:
                return str(e)
            try:
                adapter, handle = await self._conn_for(action["channel"], obs.user_id)
                sent = await actions.send(conn, action_id, adapter, handle)
            except Exception as e:  # noqa: BLE001
                await self._edit(tg_mid, f"⚠️ Send failed: {e}\nTap Send again to retry.", actions.buttons(action))
                return "Send failed"
            await self._edit(tg_mid, f"✅ Sent (id {sent['external_id']})\n\n{sent['content'].get('body', '')}", None)
            return "Sent"
        if verb == "edit":
            action = await action_repo.get(conn, action_id)
            if action is None or action["status"] not in ("awaiting_approval", "failed"):
                return "Nothing to edit"
            await action_repo.set_pending_edit(conn, obs.user_id, obs.thread_key or "", action_id)
            await self.notifier.send_text("✏️ Send the corrected text as your next message (or 'edit: ...').")
            return "Waiting for your correction"
        if verb == "reject":
            try:
                await actions.reject(conn, action_id)
            except actions.InvalidTransition as e:
                return str(e)
            await self._edit(tg_mid, "❌ Cancelled", None)
            return "Cancelled"
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
            open_actions = await action_repo.list_open(conn, obs.user_id)
            if not open_actions:
                await self.notifier.send_text("No draft to edit.")
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

    async def _edit(self, tg_message_id: int | None, text: str, markup: dict | None) -> None:
        if tg_message_id is None:
            await self.notifier.send_text(text)
            return
        try:
            await self.notifier.edit_message(tg_message_id, text, markup)
        except Exception as e:  # noqa: BLE001
            log.warning("approval.edit_message_failed", error=str(e))
            await self.notifier.send_text(text)
