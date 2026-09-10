"""Consumes Telegram inline-button callbacks (they arrive as observations from the control channel).

callback_data formats (<= 64 bytes):
  fb:useful:<sent_id>   fb:noise:<sent_id>   mute:thread:<sent_id>
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import psycopg

from core.log import get_logger
from core.models import Observation
from core.repo import budget_repo
from core.routing import callback_data

log = get_logger("workers.feedback")


def parse_callback(data: str | None) -> tuple[str, str, UUID] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return parts[0], parts[1], UUID(parts[2])
    except ValueError:
        return None


async def apply(conn: psycopg.AsyncConnection, obs: Observation) -> str | None:
    """Apply the feedback carried by a callback observation. Returns a short ack text for the user."""
    parsed = parse_callback(callback_data(obs))
    if parsed is None:
        log.warning("feedback.unparsed", data=callback_data(obs))
        return None
    action, arg, sent_id = parsed
    if action == "fb" and arg in ("useful", "noise"):
        ok = await budget_repo.set_feedback(conn, obs.user_id, sent_id, arg)
        return ("Noted: useful 👍" if arg == "useful" else "Noted: noise 👎") if ok else "Unknown notification"
    if action == "mute" and arg == "thread":
        cur = await conn.execute("select thread_key from sent_notification where id = %s and user_id = %s", (sent_id, obs.user_id))
        row = await cur.fetchone()
        if not row or not row["thread_key"]:
            return "Nothing to mute"
        await budget_repo.add_mute(conn, obs.user_id, "thread", row["thread_key"], until=None)
        return "Thread muted 🔇"
    log.warning("feedback.unknown_action", action=action, arg=arg)
    return None


def now() -> datetime:
    return datetime.now(tz=UTC)
