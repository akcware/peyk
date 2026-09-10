"""Builds GateState from the DB. The only I/O around gate.decide()."""
from __future__ import annotations

from datetime import datetime

import psycopg

from core.identity import normalize_email
from core.models import Observation
from core.repo import budget_repo
from workers.gate import BudgetSettings, GateState, MuteRule


async def load(conn: psycopg.AsyncConnection, obs: Observation, now: datetime) -> GateState:
    settings = BudgetSettings(**await budget_repo.get_budget_settings(conn, obs.user_id))
    mutes = [MuteRule(kind=m["kind"], value=m["value"], until=m["until"]) for m in await budget_repo.list_mutes(conn, obs.user_id, now)]
    sender_raw = obs.payload.get("from")
    return GateState(
        sent_today=await budget_repo.sent_today(conn, obs.user_id, now),
        last_sent_in_thread_at=await budget_repo.last_sent_in_thread(conn, obs.user_id, obs.thread_key),
        mutes=mutes,
        settings=settings,
        now=now,
        sender=normalize_email(str(sender_raw)) if sender_raw else None,
    )
