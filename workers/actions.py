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

from core.adapter import DOCUMENT_CHANNELS, SourceAdapter
from core.log import get_logger
from core.models import Content
from core.phrases import phrase, when_text
from core.repo import action_repo

log = get_logger("workers.actions")

HASH_PREFIX = 16


class ApprovalMismatch(Exception):
    """approved hash does not match the current content hash."""


class InvalidTransition(Exception):
    pass


# ---------- calendar changes: content["event"] = {"op": create|update|delete|rsvp, ...} ----------

CALENDAR_OPS = ("create", "update", "delete", "rsvp")
_CARD_HEAD = {"create": "cal_new", "update": "cal_change", "delete": "cal_delete", "rsvp": "cal_rsvp"}
_RSVP = ("accepted", "declined", "tentative")


def _op(event: dict) -> str:
    op = str(event.get("op") or "")
    return op if op in CALENDAR_OPS else "create"


def calendar_needs_confirmation(event: dict) -> bool:
    """Whatever tells other people waits for a tap: invitations, changes to shared events, cancellations and
    answers to invitations. The person's own private event is written right away, like their own document."""
    op = _op(event)
    if op == "create":
        return bool(event.get("attendees"))
    if op == "update":
        cur = event.get("current") or {}
        return not cur or bool(cur.get("guests")) or not cur.get("organized_by_me") or bool(event.get("attendees_given"))
    return True


def event_title(event: dict) -> str:
    return str(event.get("title") or (event.get("current") or {}).get("title") or "").strip() or "(no title)"


def event_when(event: dict, lang: str | None = None) -> str:
    """The event's time after the change: a new start wins over the current one."""
    cur = event.get("current") or {}
    if event.get("start"):
        return when_text(str(event["start"]), str(event.get("end") or ""), event.get("timezone"), lang)
    if cur.get("start"):
        return when_text(str(cur["start"]), str(event.get("end") or cur.get("end") or ""), event.get("timezone"), lang)
    return ""


def event_guests(event: dict) -> list[str]:
    if _op(event) == "create" or event.get("attendees_given"):
        return [str(g) for g in event.get("attendees") or []]
    return [str(g.get("name") or g.get("email")) for g in (event.get("current") or {}).get("guests") or [] if isinstance(g, dict)]


def render_event_card(event: dict, lang: str | None = None) -> str:
    """The calendar card: what, when (and when it was, for a move), who gets told."""
    op, cur = _op(event), event.get("current") or {}
    lines = [phrase(lang, _CARD_HEAD[op]), event_title(event)]
    when = event_when(event, lang)
    if when:
        lines.append(when)
    if op == "update" and event.get("start") and cur.get("start"):
        lines.append(phrase(lang, "cal_was", when=when_text(str(cur["start"]), str(cur.get("end") or ""), event.get("timezone"), lang)))
    guests = event_guests(event)
    if op in ("create", "update"):
        if guests:
            lines.append(phrase(lang, "cal_guests", guests=", ".join(guests)))
        where = event.get("location") or (cur.get("location") if op == "update" else "")
        if where:
            lines.append(phrase(lang, "cal_where", where=where))
        if event.get("meet"):
            lines.append(phrase(lang, "cal_meet"))
    if op == "rsvp" and event.get("response") in _RSVP:
        lines.append(phrase(lang, "cal_answer", answer=phrase(lang, f"rsvp_{event['response']}")))
    if guests and op != "rsvp":
        lines.append(phrase(lang, "cal_notify"))
    return "\n".join(lines)


def event_done_line(event: dict, lang: str | None = None, url: str = "") -> str:
    """What follows the agent's reply after a direct write (no card): the event as it now stands, with its link."""
    title, when = event_title(event), event_when(event, lang)
    line = phrase(lang, "cal_added", title=title, when=when, url=url) if url else phrase(lang, "cal_added_nolink", title=title, when=when)
    return line if _op(event) == "create" else f"{phrase(lang, 'cal_done_update')}\n{line}"


def render_done(action: dict, lang: str | None = None) -> str:
    """What the card turns into once the approved action went out: what happened, no ids."""
    c = action["content"]
    event = c.get("event")
    if event:
        when = event_when(event, lang)
        return f"{phrase(lang, 'cal_done_' + _op(event))}\n{event_title(event)}" + (f", {when}" if when else "")
    return f"{phrase(lang, 'sent')}\n\n{c.get('body', '')}"


def render_draft(action: dict, lang: str | None = None) -> str:
    """The approval card, in the person's language. No ids or hashes: the buttons carry them."""
    c = action["content"]
    if c.get("event"):
        return render_event_card(c["event"], lang)
    doc_label = DOCUMENT_CHANNELS.get(action.get("channel") or "")
    if doc_label:   # a document to create: subject is its title, thread_key its parent page/folder
        lines = ["📝 " + phrase(lang, "draft_doc", label=doc_label)]
        if c.get("subject"):
            lines.append(phrase(lang, "title", title=c["subject"]))
        if action.get("thread_key"):
            lines.append(phrase(lang, "parent", parent=action["thread_key"]))
    else:
        lines = ["📝 " + phrase(lang, "draft_reply" if action.get("thread_key") else "draft")]
        if c.get("to"):
            lines.append(phrase(lang, "to", to=", ".join(c["to"])))
        if c.get("subject"):
            lines.append(phrase(lang, "subject", subject=c["subject"]))
    lines += ["", c.get("body", "")]
    return "\n".join(lines)


def buttons(action: dict, lang: str | None = None) -> dict:
    aid, h = action["id"].hex, action["content_hash"][:HASH_PREFIX]   # hex uuid keeps callback_data <= 64 bytes
    event = (action.get("content") or {}).get("event")
    if event:   # no Edit: a calendar change is corrected by telling Peyk, who drafts a new card
        return {"inline_keyboard": [[
            {"text": phrase(lang, f"btn_cal_{_op(event)}"), "callback_data": f"act:approve:{aid}:{h}"},
            {"text": phrase(lang, "btn_cancel"), "callback_data": f"act:reject:{aid}:{h}"},
        ]]}
    verb = "btn_create" if action.get("channel") in DOCUMENT_CHANNELS else "btn_send"
    return {"inline_keyboard": [[
        {"text": phrase(lang, verb), "callback_data": f"act:approve:{aid}:{h}"},
        {"text": phrase(lang, "btn_edit"), "callback_data": f"act:edit:{aid}:{h}"},
        {"text": phrase(lang, "btn_cancel"), "callback_data": f"act:reject:{aid}:{h}"},
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
    extra = {"channel": a["channel"], **({"event": c["event"]} if c.get("event") else {})}
    content = Content(text=c.get("body", ""), subject=c.get("subject"), to=list(c.get("to") or []), extra=extra)
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
