"""Profile samplers: per toolkit, a cheap read of the person's recent activity used ONCE after a service is
connected, so the agent can propose what it learned (people, topics, rhythms) and ask the person to confirm.

Contract: sampler(execute, composio_user_id) -> list[dict] of compact facts, e.g.
  {"kind": "contact", "name": "...", "email": "...", "count": 12}
  {"kind": "subject", "text": "...", "from": "...", "when": "2026-09-01"}
  {"kind": "document", "title": "...", "updated": "..."}   (notion / drive / docs samplers add their own kinds)
No bodies, no attachments: metadata only. Keep each sampler to a few API calls.
`execute(slug, arguments, composio_user_id)` is ComposioAdapter._execute.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from email.utils import parseaddr
from typing import Any

ExecuteFn = Callable[[str, dict[str, Any], str | None], dict[str, Any]]


def sample_gmail(execute: ExecuteFn, composio_user_id: str, *, days: int = 30, max_messages: int = 200) -> list[dict[str, Any]]:
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
    return facts


# toolkit slug -> sampler. New services register here (notion, googledrive, googledocs, …).
PROFILE_SAMPLERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "gmail": sample_gmail,
}
