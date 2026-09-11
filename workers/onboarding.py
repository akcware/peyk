"""Agent-driven onboarding: the chat agent decides to connect a service; we create the OAuth link, show it,
and poll the connection with a scheduled job until it is ACTIVE (then enable triggers) or expires."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from adapters.composio.setup import TOOLKITS, normalize_toolkit
from core.adapter import AdapterRegistry
from core.log import get_logger
from core.repo import job_repo, user_repo

log = get_logger("workers.onboarding")

CONNECT_TIMEOUT = timedelta(minutes=15)
POLL = "every:20s"


def _composio(registry: AdapterRegistry | None):
    if registry is None:
        return None
    try:
        return registry.get("composio")
    except KeyError:
        return None


async def user_state(conn: psycopg.AsyncConnection, user: dict, registry: AdapterRegistry | None, *, refresh: bool = False) -> dict[str, Any]:
    """What the agent needs to know to onboard: connected services, pending links, whether the person is new."""
    st = dict(user.get("state") or {})
    connected: dict[str, str] = dict(st.get("connected") or {})
    adapter = _composio(registry)
    if adapter is not None and (refresh or not st.get("connected_checked_at")):
        try:
            handle = await adapter.connect(user["id"])
            connected = await adapter.connected_toolkits(handle)
            st = (await user_repo.merge_state(conn, user["id"], {"connected": connected, "connected_checked_at": datetime.now(tz=UTC).isoformat()}))["state"]
        except Exception as e:  # noqa: BLE001
            log.warning("onboarding.connected_check_failed", error=str(e))
    pending = {k: v for k, v in (st.get("pending") or {}).items()
               if datetime.fromisoformat(v["started_at"]) > datetime.now(tz=UTC) - CONNECT_TIMEOUT}
    return {
        "is_new": not (user.get("profile") or "").strip() and not connected and not st.get("greeted"),
        "connected": sorted(connected),
        "pending": sorted(pending),
        "available": sorted(TOOLKITS),
        "profile_set": bool((user.get("profile") or "").strip()),
    }


async def start_connection(conn: psycopg.AsyncConnection, user: dict, service: str, registry: AdapterRegistry | None) -> str:
    """Create the OAuth link and the polling job. Returns the message to show the person."""
    toolkit = normalize_toolkit(service)
    if toolkit is None:
        return f"I can connect: {', '.join(TOOLKITS)}."
    adapter = _composio(registry)
    if adapter is None:
        return "Connections are not available right now."
    handle = await adapter.connect(user["id"])
    already = (user.get("state") or {}).get("connected") or {}
    if toolkit in already:
        return f"{TOOLKITS[toolkit]['label']} is already connected ✅"
    link = await adapter.link(handle, toolkit)
    started = datetime.now(tz=UTC)
    await user_repo.merge_state(conn, user["id"], {"pending": {**((user.get("state") or {}).get("pending") or {}),
                                                              toolkit: {"connection_id": link["connection_id"], "started_at": started.isoformat()}}})
    await job_repo.cancel_matching(conn, user["id"], "await_connection", "toolkit", toolkit)
    await job_repo.create(conn, user["id"], run_at=started + timedelta(seconds=20), kind="await_connection",
                          payload={"toolkit": toolkit, "connection_id": link["connection_id"], "expires_at": (started + CONNECT_TIMEOUT).isoformat()},
                          recurrence=POLL, created_by="agent")
    log.info("onboarding.link_sent", user_id=str(user["id"]), toolkit=toolkit)
    return f"🔗 {TOOLKITS[toolkit]['label']}\n{link['url']}"


async def check_connection(conn: psycopg.AsyncConnection, user_id: UUID, job_payload: dict[str, Any], registry: AdapterRegistry | None,
                           notifier, react=None) -> str | None:
    """await_connection tick. Returns 'connected' | 'expired' | None (still waiting)."""
    toolkit, connection_id = job_payload.get("toolkit"), job_payload.get("connection_id")
    adapter = _composio(registry)
    user = await user_repo.get(conn, user_id)
    if adapter is None or user is None or not toolkit:
        await job_repo.cancel_matching(conn, user_id, "await_connection", "toolkit", toolkit or "")
        return None
    expired = datetime.fromisoformat(job_payload["expires_at"]) < datetime.now(tz=UTC) if job_payload.get("expires_at") else False
    handle = await adapter.connect(user_id)
    status = ""
    try:
        status = await adapter.connection_status(handle, connection_id)
    except Exception as e:  # noqa: BLE001
        log.warning("onboarding.status_failed", error=str(e))
    if status.upper() == "ACTIVE":
        connected = await adapter.connected_toolkits(handle)
        account_id = connected.get(toolkit) or connection_id
        try:
            trigger_ids = await adapter.enable_triggers(handle, toolkit, account_id)
        except Exception as e:  # noqa: BLE001
            log.error("onboarding.enable_triggers_failed", error=str(e))
            trigger_ids = []
        pending = dict((user.get("state") or {}).get("pending") or {})
        pending.pop(toolkit, None)
        await user_repo.merge_state(conn, user_id, {"connected": connected, "pending": pending, "connected_checked_at": datetime.now(tz=UTC).isoformat()})
        await job_repo.cancel_matching(conn, user_id, "await_connection", "toolkit", toolkit)
        label = TOOLKITS[toolkit]["label"]
        fallback = f"✅ {label} connected. I'm watching it now."
        if react is not None:
            await react(conn, user_id, f"{label} was just connected successfully; you are now watching it for this person", fallback)
        else:
            await notifier.send_text(fallback)
        log.info("onboarding.connected", user_id=str(user_id), toolkit=toolkit, triggers=trigger_ids)
        return "connected"
    if expired or status.upper() in ("FAILED", "EXPIRED", "REVOKED"):
        pending = dict((user.get("state") or {}).get("pending") or {})
        pending.pop(toolkit, None)
        await user_repo.merge_state(conn, user_id, {"pending": pending})
        await job_repo.cancel_matching(conn, user_id, "await_connection", "toolkit", toolkit)
        label = TOOLKITS[toolkit]["label"]
        fallback = f"⌛ The {label} link expired or failed. Say the word and I'll send a new one."
        if react is not None:
            await react(conn, user_id, f"the {label} login link expired without being completed; offer to send a new one", fallback)
        else:
            await notifier.send_text(fallback)
        return "expired"
    return None
