"""Contact lookup for the chat agent: our own identity table first, then adapters that can search contacts."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

import psycopg

from core.adapter import AdapterRegistry
from core.log import get_logger
from core.repo import identity_repo

log = get_logger("workers.contacts")

ContactSearch = Callable[[psycopg.AsyncConnection, str], Awaitable[list[dict]]]


def make_contact_search(registry: AdapterRegistry | None, user_id: UUID) -> ContactSearch:
    async def search(conn: psycopg.AsyncConnection, query: str) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for row in await identity_repo.search(conn, user_id, query):
            key = f"{row['kind']}:{row['value']}"
            if key not in seen:
                seen.add(key)
                out.append({"name": row["name"] or "", row["kind"]: row["value"], "source": "known_person"})
        if registry is not None:
            for adapter in registry.all():
                fn = getattr(adapter, "search_contacts", None)
                if fn is None:
                    continue
                try:
                    handle = await adapter.connect(user_id)
                    for c in await fn(handle, query):
                        key = f"email:{c.get('email')}"
                        if key not in seen:
                            seen.add(key)
                            out.append(c)
                except Exception as e:  # noqa: BLE001
                    log.warning("contacts.adapter_failed", adapter=adapter.id, error=str(e))
        log.info("contacts.searched", query=query, found=len(out))
        return out

    return search
