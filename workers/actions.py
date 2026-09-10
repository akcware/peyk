"""Approval state machine.

draft ──present──▶ awaiting_approval ──approve(h)──▶ approved ──send──▶ sent
                        │  ▲                              │
                        │  └── edit(new content) ─────────┘  (hash changes -> awaiting_approval again)
                        └── reject ──▶ rejected

approve() must carry the hash the user saw: an old Telegram message's button carries an old hash prefix and
is rejected ("draft changed, approve again"). send() only works on approved + matching hash and is idempotent.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from core.adapter import SourceAdapter
from core.log import get_logger
from core.models import Content
from core.repo import action_repo

log = get_logger("workers.actions")

HASH_PREFIX = 16


class ApprovalMismatch(Exception):
    """approved hash does not match the current content hash."""


class InvalidTransition(Exception):
    pass


def render_draft(action: dict) -> str:
    c = action["content"]
    head = "📝 Draft" + (" (reply in thread)" if action.get("thread_key") else "")
    lines = [head]
    if c.get("to"):
        lines.append(f"To: {', '.join(c['to'])}")
    if c.get("subject"):
        lines.append(f"Subject: {c['subject']}")
    lines += ["", c.get("body", "")]
    lines += ["", f"#{str(action['id'])[:8]} · v{action['content_hash'][:8]}"]
    return "\n".join(lines)


def buttons(action: dict) -> dict:
    aid, h = action["id"].hex, action["content_hash"][:HASH_PREFIX]   # hex uuid keeps callback_data <= 64 bytes
    return {"inline_keyboard": [[
        {"text": "✅ Send", "callback_data": f"act:approve:{aid}:{h}"},
        {"text": "✏️ Edit", "callback_data": f"act:edit:{aid}:{h}"},
        {"text": "❌ Cancel", "callback_data": f"act:reject:{aid}:{h}"},
    ]]}


async def present(conn: psycopg.AsyncConnection, action_id: UUID) -> dict:
    a = await action_repo.get(conn, action_id)
    if a is None or a["status"] not in ("draft", "awaiting_approval", "failed"):
        raise InvalidTransition(f"cannot present action in status {a and a['status']}")
    return await action_repo.set_status(conn, action_id, "awaiting_approval")


async def edit(conn: psycopg.AsyncConnection, action_id: UUID, new_content: dict[str, Any]) -> dict:
    return await action_repo.update_content(conn, action_id, new_content)


async def reject(conn: psycopg.AsyncConnection, action_id: UUID) -> dict:
    return await action_repo.set_status(conn, action_id, "rejected")


async def approve(conn: psycopg.AsyncConnection, action_id: UUID, approved_hash: str) -> dict:
    """approved_hash may be the full hash or the callback prefix; a prefix is verified against the full hash."""
    a = await action_repo.get(conn, action_id)
    if a is None:
        raise InvalidTransition("unknown action")
    if a["status"] not in ("awaiting_approval", "failed"):
        raise InvalidTransition(f"cannot approve action in status {a['status']}")
    full = a["content_hash"]
    ok = approved_hash == full or (len(approved_hash) >= HASH_PREFIX and full.startswith(approved_hash))
    if not ok:
        raise ApprovalMismatch("draft changed, approve again")
    return await action_repo.set_status(conn, action_id, "approved", approved_hash=full)


async def send(conn: psycopg.AsyncConnection, action_id: UUID, adapter: SourceAdapter, connection) -> dict:
    a = await action_repo.get(conn, action_id)
    if a is None:
        raise InvalidTransition("unknown action")
    if a["sent_at"] is not None or a["status"] == "sent":
        return a  # idempotent
    if a["status"] != "approved" or a["approved_hash"] != a["content_hash"]:
        raise InvalidTransition("send requires status=approved with approved_hash == content_hash")
    c = a["content"]
    content = Content(text=c.get("body", ""), subject=c.get("subject"), to=list(c.get("to") or []))
    try:
        external_id = await adapter.send(connection, a["thread_key"] or "", content)
    except Exception as e:
        log.error("action.send_failed", action_id=str(action_id), error=str(e))
        await action_repo.set_status(conn, action_id, "failed")  # approved_hash kept: retry needs no re-approval
        raise
    return await action_repo.set_status(conn, action_id, "sent", external_id=str(external_id), sent=True)


async def retry(conn: psycopg.AsyncConnection, action_id: UUID, adapter: SourceAdapter, connection) -> dict:
    a = await action_repo.get(conn, action_id)
    if a is None or a["status"] != "failed" or a["approved_hash"] != a["content_hash"]:
        raise InvalidTransition("retry only for failed actions whose approval is still valid")
    await action_repo.set_status(conn, action_id, "approved")
    return await send(conn, action_id, adapter, connection)


def parse_callback(data: str | None) -> tuple[str, UUID, str] | None:
    """'act:<approve|edit|reject>:<uuid hex>:<hash16>' -> (verb, id, hash)"""
    if not data or not data.startswith("act:"):
        return None
    parts = data.split(":")
    if len(parts) != 4:
        return None
    try:
        return parts[1], UUID(parts[2]), parts[3]
    except ValueError:
        return None
