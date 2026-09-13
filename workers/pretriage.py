"""Before triage: is this world event news at all? Data-driven, no source branches.

1. Review. A kind with a Review (adapters/composio/mappings.REVIEWS) is compared with the previous state of its
   thread. The review drops what is not news (the person's own edit, a replay, a change nobody needs) or says what
   changed; that answer is merged into the stored payload, so triage, the notification and the chat agent all read
   the same `change`.
2. Already told. With both Gmail and Calendar connected, one calendar change arrives twice: Google's notification
   mail and the calendar trigger. Whichever comes second, an observation about an event the person was notified
   about within ALREADY_TOLD is not news again. Only observations about a calendar event carry an `event_title`,
   so ordinary mail never takes this path, and a calendar change still arrives when Google sends no mail at all."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from adapters.composio.calendar import event_title_of
from adapters.composio.mappings import REVIEWS
from core.models import Observation
from core.repo import observation_repo, user_repo

ALREADY_TOLD = timedelta(minutes=20)   # both copies of one change arrive within minutes (trigger polls: 1-2 min)


@dataclass(frozen=True)
class Verdict:
    news: bool
    reason: str            # news | not_news | already_told
    obs: Observation       # carries the reviewed payload


async def previous_state(conn: psycopg.AsyncConnection, obs: Observation, kinds: tuple[str, ...]) -> dict[str, Any] | None:
    """The newest earlier observation of the same thread that holds the thing's state (the event before this change)."""
    if not obs.thread_key:
        return None
    cur = await conn.execute(
        "select payload from observation where user_id = %s and source = %s and thread_key = %s and id <> %s "
        "and kind = any(%s) and occurred_at < %s order by occurred_at desc limit 1",
        (obs.user_id, obs.source, obs.thread_key, obs.id, list(kinds), obs.occurred_at),
    )
    row = await cur.fetchone()
    return row["payload"] if row else None


async def told_about(conn: psycopg.AsyncConnection, obs: Observation, title: str) -> bool:
    cur = await conn.execute(
        "select 1 from sent_notification sn join observation o on o.id = sn.observation_id "
        "where o.user_id = %s and o.id <> %s and sn.sent_at >= now() - %s::interval and o.payload->>'event_title' = %s limit 1",
        (obs.user_id, obs.id, ALREADY_TOLD, title),
    )
    return await cur.fetchone() is not None


async def review(conn: psycopg.AsyncConnection, obs: Observation, now: datetime) -> Verdict:
    payload = dict(obs.payload)
    entry = REVIEWS.get(obs.kind)
    if entry is not None:
        user = await user_repo.get(conn, obs.user_id)
        found = entry.review(payload, await previous_state(conn, obs, entry.state_kinds), set(user_repo.own_emails(user)), now)
        if found is None:
            return Verdict(False, "not_news", obs)
        payload.update(found)
    title = event_title_of(payload)
    if title:
        payload["event_title"] = title
    if payload != obs.payload:
        await observation_repo.update_payload(conn, obs.id, payload)
        obs = obs.model_copy(update={"payload": payload})
    if title and await told_about(conn, obs, title):
        return Verdict(False, "already_told", obs)
    return Verdict(True, "news", obs)
