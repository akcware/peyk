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

from core.adapter import Capabilities, NotSupported, UserDirectory
from core.config import Settings, get_settings
from core.log import get_logger
from core.models import Connection, Content, Observation

from .documents import CREATORS, DOCUMENT_SERVICES, READERS, SEARCHERS
from .mappings import ACTION_ITEMS_PATH, ACTION_MAPPINGS, ACTION_NEXT_PAGE_PATH, apply, get_path
from .profile import PROFILE_SAMPLERS
from .setup import TOOLKITS
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
        users: UserDirectory | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._execute_override = execute
        self._page_size = page_size
        self._users = users

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
            # Bounded: a slow Composio call must never hold a chat turn for minutes (20 s per request, one retry).
            self._client = Composio(api_key=self._settings.COMPOSIO_API_KEY, toolkit_versions=versions or None,
                                    timeout=20.0, max_retries=1)
        return self._client

    def _execute(self, slug: str, arguments: dict[str, Any], composio_user_id: str | None = None) -> dict[str, Any]:
        uid = composio_user_id or self._settings.COMPOSIO_USER_ID
        if self._execute_override is not None:
            return self._execute_override(slug, arguments, user_id=uid)
        return self.client.tools.execute(slug, arguments, user_id=uid)

    async def _composio_user_id(self, user_id: UUID) -> str:
        if self._users is not None:
            try:
                return await self._users.composio_user_id(user_id)
            except Exception as e:  # noqa: BLE001
                log.warning("composio.user_lookup_failed", user_id=str(user_id), error=str(e))
        return self._settings.COMPOSIO_USER_ID

    # ---- SourceAdapter ----
    def capabilities(self) -> Capabilities:
        return {"can_send": True, "needs_session": False, "needs_user_device": False}

    async def connect(self, user_id: UUID) -> Connection:
        """Cheap: no network. The per-user Composio entity id is what every later call needs."""
        cuid = await self._composio_user_id(user_id)
        conn = Connection(adapter_id=self.id, user_id=user_id, data={"composio_user_id": cuid})
        if not self._settings.COMPOSIO_API_KEY:
            log.warning("composio.no_api_key", hint="set COMPOSIO_API_KEY; adapter runs in inert mode")
        return conn

    # ---- onboarding (driven by the agent through workers) ----
    async def connected_toolkits(self, conn: Connection) -> dict[str, str]:
        """{toolkit_slug: connected_account_id} for ACTIVE accounts of this user."""
        accounts = await asyncio.to_thread(
            self.client.client.connected_accounts.list, user_ids=[conn.data["composio_user_id"]], statuses=["ACTIVE"])
        out: dict[str, str] = {}
        for a in getattr(accounts, "items", None) or []:
            slug = getattr(getattr(a, "toolkit", None), "slug", None) or getattr(a, "toolkit_slug", None) or ""
            if slug and slug not in out:
                out[slug] = a.id
        return out

    async def link(self, conn: Connection, toolkit: str) -> dict[str, str]:
        """Start OAuth for a toolkit: returns {url, connection_id}. The user opens the url; see connection_status()."""
        cfg = TOOLKITS.get(toolkit)
        if cfg is None:
            raise NotSupported(f"unknown toolkit {toolkit!r}")
        auth_config_id = getattr(self._settings, cfg["auth_config_env"], "") or self._env(cfg["auth_config_env"])
        if not auth_config_id:
            raise RuntimeError(f"{cfg['auth_config_env']} not configured")
        req = await asyncio.to_thread(self.client.connected_accounts.link, conn.data["composio_user_id"], auth_config_id)
        return {"url": req.redirect_url, "connection_id": req.id}

    async def connection_status(self, conn: Connection, connection_id: str) -> str:
        acc = await asyncio.to_thread(self.client.client.connected_accounts.retrieve, connection_id)
        return str(getattr(acc, "status", "") or "")

    # ---- documents (notion / drive / docs) ----
    async def search_documents(self, conn: Connection, query: str, services: list[str] | None = None) -> list[dict[str, Any]]:
        connected = await self.connected_toolkits(conn)
        targets = [s for s in (services or DOCUMENT_SERVICES) if s in connected and s in SEARCHERS]
        out: list[dict[str, Any]] = []
        for svc in targets:
            try:
                out += await asyncio.to_thread(SEARCHERS[svc], self._execute, conn.data.get("composio_user_id"), query)
            except Exception as e:  # noqa: BLE001
                log.warning("composio.search_documents_failed", service=svc, error=str(e))
        return out

    async def read_document(self, conn: Connection, service: str, doc_id: str) -> dict[str, Any]:
        reader = READERS.get(service)
        if reader is None:
            raise NotSupported(f"cannot read documents from {service!r}")
        return await asyncio.to_thread(reader, self._execute, conn.data.get("composio_user_id"), doc_id)

    async def create_document(self, conn: Connection, service: str, title: str, body_markdown: str, parent: str | None = None) -> dict[str, Any]:
        creator = CREATORS.get(service)
        if creator is None:
            raise NotSupported(f"cannot create documents in {service!r}")
        return await asyncio.to_thread(creator, self._execute, conn.data.get("composio_user_id"), title, body_markdown, parent)

    async def sample_for_profile(self, conn: Connection, toolkit: str) -> list[dict[str, Any]]:
        """Metadata sample of the person's recent activity in a toolkit (see profile.py). [] if none registered."""
        sampler = PROFILE_SAMPLERS.get(toolkit)
        if sampler is None:
            return []
        return await asyncio.to_thread(sampler, self._execute, conn.data.get("composio_user_id"))

    async def disconnect_all(self, conn: Connection) -> dict[str, int]:
        """Right-to-erasure on the Composio side: delete this user's trigger instances and connected accounts
        (revoking the Google grant where Composio supports it)."""
        cuid = conn.data["composio_user_id"]
        accounts = await asyncio.to_thread(self.client.client.connected_accounts.list, user_ids=[cuid])
        ids = [a.id for a in (getattr(accounts, "items", None) or [])]
        removed = {"triggers": 0, "accounts": 0}
        if ids:
            try:
                act = await asyncio.to_thread(self.client.triggers.list_active, connected_account_ids=ids, show_disabled=True)
                for t in getattr(act, "items", None) or []:
                    await asyncio.to_thread(self.client.client.trigger_instances.manage.delete, t.id)
                    removed["triggers"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("composio.trigger_delete_failed", error=str(e))
        for aid in ids:
            try:
                await asyncio.to_thread(self.client.client.connected_accounts.delete, aid, revoke_on_delete=True)
                removed["accounts"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("composio.account_delete_failed", account_id=aid, error=str(e))
        log.info("composio.disconnected_all", composio_user_id=cuid, **removed)
        return removed

    async def enable_triggers(self, conn: Connection, toolkit: str, connected_account_id: str) -> list[str]:
        cfg = TOOLKITS.get(toolkit) or {}
        ids: list[str] = []
        for slug, trigger_config in (cfg.get("triggers") or {}).items():
            r = await asyncio.to_thread(self.client.triggers.create, slug, user_id=conn.data["composio_user_id"],
                                        connected_account_id=connected_account_id, trigger_config=trigger_config)
            ids.append(str(getattr(r, "trigger_id", r)))
        return ids

    @staticmethod
    def _env(name: str) -> str:
        import os

        return os.environ.get(name, "")

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

        async def _watchdog() -> None:
            while True:
                await asyncio.sleep(15)
                alive = getattr(subscription, "is_alive", lambda: True)()
                errored = getattr(subscription, "has_errored", lambda: False)()
                if not alive or errored:
                    log.warning("composio.subscription_dead", alive=alive, errored=errored)
                    queue.put_nowait(_QUEUE_END)
                    return

        watchdog = asyncio.create_task(_watchdog(), name="composio-watchdog")
        try:
            while True:
                event = await queue.get()
                if event is _QUEUE_END:
                    raise ConnectionError("composio realtime subscription ended; reconnecting")
                user_id = await self.user_for_event(event, default=conn.user_id)
                if user_id is None:
                    log.warning("composio.event_unknown_user", composio_user_id=event.get("user_id"), trigger=event.get("trigger_slug"))
                    continue
                obs = to_observation(event, user_id)
                if obs is not None:
                    yield obs
        finally:
            watchdog.cancel()
            try:
                subscription.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("composio.subscription_stop_failed", error=str(e))

    async def user_for_event(self, event: dict[str, Any], *, default: UUID | None) -> UUID | None:
        """Composio events carry the entity user id; map it to our user. The legacy single-user entity id
        (COMPOSIO_USER_ID from .env) maps to the bootstrap user."""
        cuid = str(event.get("user_id") or ((event.get("metadata") or {}).get("connected_account") or {}).get("user_id") or "")
        if self._users is not None and cuid:
            resolved = await self._users.resolve_composio(cuid)
            if resolved is not None:
                return resolved
        if not cuid or cuid == self._settings.COMPOSIO_USER_ID:
            return default
        return None

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
            resp = await asyncio.to_thread(self._execute, slug, args, conn.data.get("composio_user_id"))
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
                "query": query, "page_size": limit, "other_contacts": True, "person_fields": "names,emailAddresses"},
                conn.data.get("composio_user_id"))
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
                    "query": f'"{query}"', "max_results": 20, "verbose": False, "include_payload": False},
                    conn.data.get("composio_user_id"))
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
        if channel in CREATORS:   # "send" = create the approved document
            created = await self.create_document(conn, channel, content.subject or "Untitled", content.text, thread_key or None)
            return str(created.get("id") or "")
        if channel != "gmail":
            raise NotSupported(f"composio adapter cannot send to {channel!r}")
        if thread_key:
            args: dict[str, Any] = {"thread_id": thread_key, "message_body": content.text, "user_id": "me"}
            if content.to:
                args["recipient_email"] = content.to[0]
            resp = await asyncio.to_thread(self._execute, "GMAIL_REPLY_TO_THREAD", args, conn.data.get("composio_user_id"))
        else:
            if not content.to:
                raise ValueError("a new mail needs at least one recipient")
            args = {"recipient_email": content.to[0], "subject": content.subject or "", "body": content.text, "user_id": "me"}
            if len(content.to) > 1:
                args["cc"] = content.to[1:]
            resp = await asyncio.to_thread(self._execute, "GMAIL_SEND_EMAIL", args, conn.data.get("composio_user_id"))
        if not resp.get("successful", True):
            raise RuntimeError(f"composio send failed: {resp.get('error')}")
        data = resp.get("data") or {}
        return str(data.get("id") or data.get("messageId") or (data.get("response_data") or {}).get("id") or "")
