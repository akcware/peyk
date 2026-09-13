"""Profile samplers: per toolkit, a cheap read of the person's recent activity used ONCE after a service is
connected, so the agent can propose what it learned (people, topics, rhythms) and ask the person to confirm.

Contract: sampler(execute, composio_user_id) -> list[dict] of compact facts, e.g.
  {"kind": "contact", "name": "...", "email": "...", "count": 12}
  {"kind": "subject", "text": "...", "from": "...", "when": "2026-09-01"}
  {"kind": "document", "title": "...", "updated": "..."}   (notion / drive / docs / calendar samplers add their own kinds)
No bodies, no attachments: metadata only. Keep each sampler to a few API calls.
`execute(slug, arguments, composio_user_id)` is ComposioAdapter._execute.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from email.utils import parseaddr
from typing import Any

from core.log import get_logger

from .calendar import sample_googlecalendar

log = get_logger("composio.profile")

ExecuteFn = Callable[[str, dict[str, Any], str | None], dict[str, Any]]


def sample_gmail(execute: ExecuteFn, composio_user_id: str, *, days: int = 30, max_messages: int = 100) -> list[dict[str, Any]]:
    """Last `days` of inbox + sent metadata: who writes to the person, whom they write to, what about."""
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(tz=UTC) - timedelta(days=days)).strftime("%Y/%m/%d")
    facts: list[dict[str, Any]] = []
    for query, direction in ((f"after:{since} in:inbox -category:promotions", "in"), (f"after:{since} in:sent", "out")):
        page_token, fetched = None, 0
        counter: Counter[tuple[str, str]] = Counter()
        while fetched < max_messages:
            args: dict[str, Any] = {"query": query, "max_results": min(100, max_messages - fetched), "verbose": False, "include_payload": False}
            if page_token:
                args["page_token"] = page_token
            resp = execute("GMAIL_FETCH_EMAILS", args, composio_user_id)
            data = resp.get("data") or {}
            msgs = data.get("messages") or []
            for m in msgs:
                fetched += 1
                if direction == "out" and m.get("sender") and not any(f.get("kind") == "self" for f in facts):
                    _, me = parseaddr(str(m["sender"]))
                    if me:
                        facts.append({"kind": "self", "email": me.lower()})
                who = m.get("to") if direction == "out" else m.get("sender")
                name, email = parseaddr(str(who or ""))
                email = email.lower()
                if email:
                    counter[(name.strip(), email)] += 1
                if direction == "in" and m.get("subject"):
                    facts.append({"kind": "subject", "text": str(m["subject"])[:120], "from": name.strip() or email,
                                  "when": str(m.get("messageTimestamp") or "")[:10]})
            page_token = data.get("nextPageToken")
            if not page_token or not msgs:
                break
        for (name, email), n in counter.most_common(12):
            facts.append({"kind": "contact", "direction": direction, "name": name, "email": email, "count": n})
    if not any(f.get("kind") == "self" for f in facts):
        # nothing sent recently: ask Gmail who the mailbox owner is, so their own mail is never read as someone writing to them
        try:
            me = (execute("GMAIL_GET_PROFILE", {}, composio_user_id).get("data") or {}).get("emailAddress")
            if me:
                facts.append({"kind": "self", "email": str(me).lower()})
        except Exception as e:  # noqa: BLE001 - optional: the next mail they send teaches the address too
            log.warning("profile.gmail_owner_failed", error=str(e))
    return facts


def _doc_facts(service: str, items: list[dict[str, Any]], *, cap: int = 40) -> list[dict[str, Any]]:
    from .documents import _first, _notion_title

    out = []
    for it in items[:cap]:
        title = _notion_title(it) if service == "notion" else _first(it, "name", "title", default="(untitled)")
        out.append({"kind": "document", "service": service, "title": title[:120],
                    "updated": _first(it, "last_edited_time", "modifiedTime", "modified_time", "updated_at")[:10]})
    return out


def sample_notion(execute: ExecuteFn, composio_user_id: str) -> list[dict[str, Any]]:
    from .documents import _data, _items

    facts: list[dict[str, Any]] = []
    pages = _data(execute("NOTION_SEARCH_NOTION_PAGE", {"query": "", "page_size": 40, "direction": "descending", "timestamp": "last_edited_time"}, composio_user_id))
    facts += _doc_facts("notion", _items(pages, "results", "pages", "items"))
    dbs = _data(execute("NOTION_FETCH_DATA", {"query": "", "fetch_type": "databases", "page_size": 20}, composio_user_id))
    for d in _items(dbs, "results", "databases", "items")[:20]:
        from .documents import _notion_title

        facts.append({"kind": "database", "service": "notion", "title": _notion_title(d)[:120]})
    return facts


def sample_googledrive(execute: ExecuteFn, composio_user_id: str) -> list[dict[str, Any]]:
    from .documents import _data, _items

    data = _data(execute("GOOGLEDRIVE_LIST_FILES", {"q": "trashed = false", "pageSize": 40, "orderBy": "modifiedTime desc",
                                                    "fields": "files(id,name,mimeType,modifiedTime)"}, composio_user_id))
    return _doc_facts("googledrive", _items(data, "files", "items"))


def sample_googledocs(execute: ExecuteFn, composio_user_id: str) -> list[dict[str, Any]]:
    from .documents import _data, _items

    data = _data(execute("GOOGLEDOCS_SEARCH_DOCUMENTS", {"query": "", "max_results": 40, "order_by": "modifiedTime desc"}, composio_user_id))
    return _doc_facts("googledocs", _items(data, "documents", "files", "items", "results"))


# toolkit slug -> sampler. New services register here.
PROFILE_SAMPLERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "gmail": sample_gmail,
    "googlecalendar": sample_googlecalendar,
    "notion": sample_notion,
    "googledrive": sample_googledrive,
    "googledocs": sample_googledocs,
}
