"""ComposioAdapter: the only place the `composio` SDK is imported (besides scripts/).

- connect():   checks a Gmail connected account exists for COMPOSIO_USER_ID; prints the dashboard hint if not.
- subscribe(): COMPOSIO_DELIVERY=ws -> triggers.subscribe() (Pusher websocket, thread -> asyncio.Queue).
               COMPOSIO_DELIVERY=webhook -> empty iterator; events arrive through gateway/ instead.
- backfill():  GMAIL_FETCH_EMAILS(query="after:YYYY/MM/DD") paged; is_backfill flag is a parameter so
               phase-2 reconcile can reuse it with is_backfill=False.
- send():      phase 4.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from core.adapter import Capabilities, NotSupported
from core.config import Settings, get_settings
from core.log import get_logger
from core.models import Connection, Content, Observation

from .mappings import ACTION_ITEMS_PATH, ACTION_MAPPINGS, ACTION_NEXT_PAGE_PATH, apply, get_path
from .webhook import to_observation

log = get_logger("composio.adapter")

ExecuteFn = Callable[..., dict[str, Any]]  # (slug, arguments, *, user_id) -> response dict
_QUEUE_END = object()


class ComposioAdapter:
    id = "composio"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: Any | None = None,
        execute: ExecuteFn | None = None,
        page_size: int = 100,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._execute_override = execute
        self._page_size = page_size

    # ---- SDK access (lazy so tests never need an API key) ----
    @property
    def client(self) -> Any:
        if self._client is None:
            from composio import Composio  # imported lazily; only this package may import composio

            versions = self._settings.composio_toolkit_versions
            if not versions:
                log.warning("composio.toolkit_versions_unpinned",
                            hint="set COMPOSIO_TOOLKIT_VERSIONS=gmail=...,googlecalendar=... (see scripts/composio_setup.py --status); "
                                 "tools.execute will fail without it")
            self._client = Composio(api_key=self._settings.COMPOSIO_API_KEY, toolkit_versions=versions or None)
        return self._client

    def _execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._execute_override is not None:
            return self._execute_override(slug, arguments, user_id=self._settings.COMPOSIO_USER_ID)
        return self.client.tools.execute(slug, arguments, user_id=self._settings.COMPOSIO_USER_ID)

    # ---- SourceAdapter ----
    def capabilities(self) -> Capabilities:
        return {"can_send": True, "needs_session": False, "needs_user_device": False}

    async def connect(self, user_id: UUID) -> Connection:
        conn = Connection(adapter_id=self.id, user_id=user_id, data={"composio_user_id": self._settings.COMPOSIO_USER_ID})
        if not self._settings.COMPOSIO_API_KEY:
            log.warning("composio.no_api_key", hint="set COMPOSIO_API_KEY; adapter runs in inert mode")
            return conn
        try:
            accounts = await asyncio.to_thread(
                self.client.client.connected_accounts.list,
                user_ids=[self._settings.COMPOSIO_USER_ID],
                toolkit_slugs=["gmail"],
                statuses=["ACTIVE"],
            )
            items = getattr(accounts, "items", None) or []
            if not items:
                log.warning(
                    "composio.gmail_not_connected",
                    hint="connect Gmail in the Composio dashboard for this user id, then enable GMAIL_NEW_GMAIL_MESSAGE",
                    composio_user_id=self._settings.COMPOSIO_USER_ID,
                )
            else:
                conn.data["gmail_connected_account_id"] = getattr(items[0], "id", None)
                log.info("composio.gmail_connected", connected_account_id=conn.data["gmail_connected_account_id"])
        except Exception as e:  # noqa: BLE001 - connectivity check must not crash workers
            log.warning("composio.connect_check_failed", error=str(e))
        return conn

    async def subscribe(self, conn: Connection) -> AsyncIterator[Observation]:
        if self._settings.COMPOSIO_DELIVERY != "ws" or not self._settings.COMPOSIO_API_KEY:
            log.info("composio.subscribe_noop", delivery=self._settings.COMPOSIO_DELIVERY,
                     has_api_key=bool(self._settings.COMPOSIO_API_KEY))
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        subscription = await asyncio.to_thread(self.client.triggers.subscribe)

        @subscription.handle()
        def _on_event(event: dict[str, Any]) -> None:  # runs on the pusher thread
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        log.info("composio.subscribed", delivery="ws")
        try:
            while True:
                event = await queue.get()
                if event is _QUEUE_END:
                    return
                obs = to_observation(event, conn.user_id)
                if obs is not None:
                    yield obs
        finally:
            try:
                subscription.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("composio.subscription_stop_failed", error=str(e))

    async def backfill(
        self, conn: Connection, since: datetime, *, is_backfill: bool = True, slug: str = "GMAIL_FETCH_EMAILS"
    ) -> AsyncIterator[Observation]:
        mapping = ACTION_MAPPINGS[slug]
        items_path = ACTION_ITEMS_PATH[slug]
        next_path = ACTION_NEXT_PAGE_PATH[slug]
        page_token: str | None = None
        while True:
            args: dict[str, Any] = {
                "query": f"after:{since:%Y/%m/%d}",
                "max_results": self._page_size,
                "verbose": False,
                "include_payload": False,
            }
            if page_token:
                args["page_token"] = page_token
            resp = await asyncio.to_thread(self._execute, slug, args)
            if not resp.get("successful", True):
                raise RuntimeError(f"{slug} failed: {resp.get('error')}")
            data = resp.get("data") or {}
            for item in get_path(data, items_path) or []:
                mapped = apply(mapping, item)
                if mapped is None:
                    continue
                yield Observation(user_id=conn.user_id, is_backfill=is_backfill, **mapped)
            page_token = get_path(data, next_path)
            if not page_token:
                return

    async def search_contacts(self, conn: Connection, query: str, *, limit: int = 8) -> list[dict[str, str]]:
        """Find people by name via Google Contacts (incl. 'other contacts' = anyone the user has mailed) and,
        as a fallback, the From/To headers of mails matching the query. Returns [{name, email, source}]."""
        found: dict[str, dict[str, str]] = {}
        try:
            resp = await asyncio.to_thread(self._execute, "GMAIL_SEARCH_PEOPLE", {
                "query": query, "page_size": limit, "other_contacts": True, "person_fields": "names,emailAddresses"})
            for item in (resp.get("data") or {}).get("results") or (resp.get("data") or {}).get("people") or []:
                person = item.get("person", item)
                names = [n.get("displayName") for n in person.get("names") or [] if n.get("displayName")]
                for e in person.get("emailAddresses") or []:
                    email = (e.get("value") or "").strip().lower()
                    if email and email not in found:
                        found[email] = {"name": names[0] if names else "", "email": email, "source": "google_contacts"}
        except Exception as e:  # noqa: BLE001 - contacts are optional, mail headers still work
            log.warning("composio.search_people_failed", error=str(e))
        if len(found) < limit:
            try:
                from email.utils import getaddresses

                resp = await asyncio.to_thread(self._execute, "GMAIL_FETCH_EMAILS", {
                    "query": f'"{query}"', "max_results": 20, "verbose": False, "include_payload": False})
                for m in (resp.get("data") or {}).get("messages") or []:
                    for name, email in getaddresses([str(m.get("sender") or ""), str(m.get("to") or "")]):
                        email = email.strip().lower()
                        if not email or email in found:
                            continue
                        if any(t in (name + " " + email).lower() for t in query.lower().split()):
                            found[email] = {"name": name, "email": email, "source": "mail_headers"}
            except Exception as e:  # noqa: BLE001
                log.warning("composio.header_search_failed", error=str(e))
        return list(found.values())[:limit]

    async def send(self, conn: Connection, thread_key: str, content: Content) -> str:
        """Gmail only. thread_key -> GMAIL_REPLY_TO_THREAD, else GMAIL_SEND_EMAIL. Returns the Gmail message id."""
        channel = content.extra.get("channel", "gmail")
        if channel != "gmail":
            raise NotSupported(f"composio adapter cannot send to {channel!r}")
        if thread_key:
            args: dict[str, Any] = {"thread_id": thread_key, "message_body": content.text, "user_id": "me"}
            if content.to:
                args["recipient_email"] = content.to[0]
            resp = await asyncio.to_thread(self._execute, "GMAIL_REPLY_TO_THREAD", args)
        else:
            if not content.to:
                raise ValueError("a new mail needs at least one recipient")
            args = {"recipient_email": content.to[0], "subject": content.subject or "", "body": content.text, "user_id": "me"}
            if len(content.to) > 1:
                args["cc"] = content.to[1:]
            resp = await asyncio.to_thread(self._execute, "GMAIL_SEND_EMAIL", args)
        if not resp.get("successful", True):
            raise RuntimeError(f"composio send failed: {resp.get('error')}")
        data = resp.get("data") or {}
        return str(data.get("id") or data.get("messageId") or (data.get("response_data") or {}).get("id") or "")
