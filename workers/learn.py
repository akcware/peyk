"""First-learn: right after a service is connected, sample its metadata, register the people in it, let the
agent propose a few facts + a profile, and ask the person to confirm. Confirmation arrives as a LearnConfirm
intent from the chat agent (tool confirm_learned) and is applied here."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from core.adapter import AdapterRegistry
from core.log import get_logger
from core.models import Observation
from core.repo import identity_repo, job_repo, memory_repo, observation_repo, user_repo

log = get_logger("workers.learn")

PENDING_TTL = timedelta(hours=24)


async def schedule(conn: psycopg.AsyncConnection, user_id: UUID, toolkit: str, *, delay_s: int = 5) -> None:
    await job_repo.create(conn, user_id, run_at=datetime.now(tz=UTC) + timedelta(seconds=delay_s), kind="first_learn",
                          payload={"toolkit": toolkit}, created_by="system")


async def run(conn: psycopg.AsyncConnection, user_id: UUID, toolkit: str, *, registry: AdapterRegistry | None, agent, notifier,
              embedder=None) -> dict[str, Any] | None:
    user = await user_repo.get(conn, user_id)
    if user is None or registry is None:
        return None
    try:
        adapter = registry.get("composio")
    except KeyError:
        return None
    handle = await adapter.connect(user_id)
    try:
        facts = await adapter.sample_for_profile(handle, toolkit)
    except Exception as e:  # noqa: BLE001
        log.warning("learn.sample_failed", toolkit=toolkit, error=str(e))
        return None
    if not facts:
        log.info("learn.nothing_to_sample", toolkit=toolkit)
        return None
    # people the person deals with -> identity table (so contact lookup and sender context work from day one)
    for f in facts:
        if f.get("kind") == "contact" and f.get("email"):
            try:
                await identity_repo.resolve(conn, user_id, "email", f["email"], display_name=f.get("name") or None)
            except Exception as e:  # noqa: BLE001
                log.debug("learn.identity_failed", error=str(e))
    payload = {"service": toolkit, "facts": facts,
               "user": {"display_name": user.get("display_name"), "profile": user.get("profile") or "", "language": user.get("language") or "en"}}
    result = await agent.learn(payload)
    proposed = [str(x).strip() for x in (result.get("facts") or []) if str(x).strip()]
    pending = {"toolkit": toolkit, "facts": proposed, "profile_suggestion": (result.get("profile_suggestion") or "").strip(),
               "top_people": result.get("top_people") or [], "at": datetime.now(tz=UTC).isoformat()}
    await user_repo.merge_state(conn, user_id, {"pending_learn": pending})
    message = (result.get("message") or "").strip()
    if message:
        send = getattr(notifier, "send_rich", None) or notifier.send_text
        mid = await send(message)
        await observation_repo.insert(conn, Observation(
            user_id=user_id, source=user["control_source"], source_key=f"out:{mid}", kind="message_out",
            occurred_at=datetime.now(tz=UTC), thread_key=user["control_thread_key"],
            payload={"text": message, "kind": "learn_proposal", "toolkit": toolkit}))
    log.info("learn.proposed", user_id=str(user_id), toolkit=toolkit, facts=len(proposed))
    return pending


def pending_for_state(user: dict) -> dict[str, Any] | None:
    """The unconfirmed proposal (if fresh) for the chat agent's user_state."""
    p = (user.get("state") or {}).get("pending_learn")
    if not p:
        return None
    try:
        if datetime.fromisoformat(p["at"]) < datetime.now(tz=UTC) - PENDING_TTL:
            return None
    except (KeyError, ValueError):
        return None
    return {"toolkit": p.get("toolkit"), "facts": p.get("facts") or [], "profile_suggestion": p.get("profile_suggestion") or ""}


async def confirm(conn: psycopg.AsyncConnection, obs: Observation, intent: dict[str, Any], *, embedder) -> int:
    """Apply a LearnConfirm intent: accepted facts -> memory, profile -> app_user, clear the proposal."""
    facts = [str(f).strip() for f in (intent.get("facts") or []) if str(f).strip()]
    written = 0
    for f in facts:
        try:
            vec = await embedder.embed(f)
            await memory_repo.insert(conn, obs.user_id, f, vec, source_observation_id=obs.id)
            written += 1
        except Exception as e:  # noqa: BLE001
            log.warning("learn.memory_failed", error=str(e))
    profile = (intent.get("profile") or "").strip()
    if profile:
        await user_repo.update(conn, obs.user_id, profile=profile)
    await user_repo.merge_state(conn, obs.user_id, {"pending_learn": None})
    log.info("learn.confirmed", user_id=str(obs.user_id), facts=written, profile=bool(profile))
    return written
