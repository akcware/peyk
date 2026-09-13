"""Keeps a connected Google Calendar ready for change news, once per SYNC_VERSION:
- its triggers exist (adapter.enable_triggers is idempotent, so people who connected before the change trigger
  existed get it too),
- the calendar owner's address is one of the person's own addresses, so their own edits are never news,
- every event of the coming weeks has a baseline state, so the first change Composio reports can be compared.
Runs from the reconcile tick (a cheap no-op once done) and right after the calendar gets connected."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg

from core.adapter import AdapterRegistry
from core.log import get_logger
from core.repo import observation_repo, user_repo

log = get_logger("workers.calendar_sync")

TOOLKIT = "googlecalendar"
SYNC_VERSION = 1
LOOK_BACK = timedelta(days=1)
LOOK_AHEAD = timedelta(days=60)


async def ensure(conn: psycopg.AsyncConnection, user_id: UUID, registry: AdapterRegistry | None, *, force: bool = False) -> bool:
    """True when the setup ran now; False when there is nothing to do (not connected, already done, no adapter).
    A failure raises and leaves the state unmarked, so the next reconcile tries again."""
    user = await user_repo.get(conn, user_id)
    state = (user or {}).get("state") or {}
    account_id = (state.get("connected") or {}).get(TOOLKIT)
    if registry is None or not account_id or (state.get("calendar_sync") == SYNC_VERSION and not force):
        return False
    try:
        adapter = registry.get("composio")
    except KeyError:
        return False
    handle = await adapter.connect(user_id)
    created = await adapter.enable_triggers(handle, TOOLKIT, account_id)
    try:
        owner = await adapter.calendar_owner(handle)
    except Exception as e:  # noqa: BLE001 - the organizer flags in the snapshot still tell us
        log.warning("calendar_sync.owner_failed", error=str(e)[:200])
        owner = ""
    now = datetime.now(tz=UTC)
    snapshots = await adapter.calendar_snapshot(handle, (now - LOOK_BACK).isoformat(), (now + LOOK_AHEAD).isoformat())
    own = {owner} if owner else set()
    own |= {str(s.payload["organizer_email"]) for s in snapshots if s.payload.get("organizer_self") and s.payload.get("organizer_email")}
    for email in sorted(own):
        await user_repo.add_own_email(conn, user_id, email)
    stored = 0
    for s in snapshots:
        stored += (await observation_repo.insert(conn, s)) is not None
    await user_repo.merge_state(conn, user_id, {"calendar_sync": SYNC_VERSION})
    log.info("calendar_sync.done", user_id=str(user_id), triggers_created=len(created), own_addresses=len(own), snapshots=stored)
    return True
