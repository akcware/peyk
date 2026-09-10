"""Declarative trigger/action -> Observation mappings. Source = data: adding a Composio source is a row here.

Paths are simple dotted JSONPaths ('$.a.b.c'). Field names were taken from Composio docs for
GMAIL_NEW_GMAIL_MESSAGE / GMAIL_FETCH_EMAILS; verify against the first real payload
(tests/fixtures/composio_events/*.raw.json) and adjust here, nowhere else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Mapping:
    source: str
    kind: str
    source_key: str                      # path to the source's natural id (gmail message id, event id)
    occurred_at: str                     # path to timestamp (ISO string or epoch ms)
    thread_key: str | None = None        # path
    payload_fields: dict[str, str] = field(default_factory=dict)   # observation payload key -> path
    source_key_template: str | None = None   # optional "{event_id}:{start}" style composition


# Triggers (inbound events). Keyed by Composio trigger slug.
MAPPINGS: dict[str, Mapping] = {
    "GMAIL_NEW_GMAIL_MESSAGE": Mapping(
        source="gmail",
        kind="message_in",
        source_key="$.message_id",
        occurred_at="$.message_timestamp",
        thread_key="$.thread_id",
        payload_fields={
            "from": "$.sender",
            "to": "$.to",
            "subject": "$.subject",
            "snippet": "$.preview.body",
            "text": "$.message_text",
            "label_ids": "$.label_ids",
        },
    ),
}

# Actions whose results are turned into observations (backfill / reconcile). Keyed by action slug.
# Path is relative to a single item of `items` inside the action's `data`.
ACTION_MAPPINGS: dict[str, Mapping] = {
    "GMAIL_FETCH_EMAILS": Mapping(
        source="gmail",
        kind="message_in",
        source_key="$.messageId",
        occurred_at="$.messageTimestamp",
        thread_key="$.threadId",
        payload_fields={
            "from": "$.sender",
            "to": "$.to",
            "subject": "$.subject",
            "snippet": "$.preview.body",
            "text": "$.messageText",
            "label_ids": "$.labelIds",
        },
    ),
}
ACTION_ITEMS_PATH: dict[str, str] = {"GMAIL_FETCH_EMAILS": "$.messages"}
ACTION_NEXT_PAGE_PATH: dict[str, str] = {"GMAIL_FETCH_EMAILS": "$.nextPageToken"}


def get_path(obj: Any, path: str) -> Any:
    """Resolve '$.a.b.c' against nested dicts/lists. Missing -> None. No wildcards, by design."""
    if not path.startswith("$"):
        raise ValueError(f"path must start with '$': {path!r}")
    cur = obj
    for part in path[1:].split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def parse_timestamp(value: Any) -> datetime | None:
    """ISO-8601 string (with or without Z), epoch seconds or ms. None if unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return parse_timestamp(int(s))
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def apply(mapping: Mapping, data: dict[str, Any]) -> dict[str, Any] | None:
    """Map raw data to Observation kwargs (minus user_id). None if the natural id is missing."""
    if mapping.source_key_template:
        try:
            source_key = mapping.source_key_template.format(**{
                k: get_path(data, v) for k, v in mapping.payload_fields.items()
            } | {"source_key": get_path(data, mapping.source_key)})
        except KeyError:
            source_key = None
    else:
        source_key = get_path(data, mapping.source_key)
    if not source_key:
        return None
    occurred_at = parse_timestamp(get_path(data, mapping.occurred_at)) or datetime.now(tz=UTC)
    thread_key = get_path(data, mapping.thread_key) if mapping.thread_key else None
    payload = {k: get_path(data, p) for k, p in mapping.payload_fields.items()}
    payload = {k: v for k, v in payload.items() if v is not None}
    return {
        "source": mapping.source,
        "kind": mapping.kind,
        "source_key": str(source_key),
        "occurred_at": occurred_at,
        "thread_key": str(thread_key) if thread_key is not None else None,
        "payload": payload,
    }
