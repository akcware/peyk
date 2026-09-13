"""Google Calendar over Composio: read (events in a window, free time) for the chat agent, write (create,
update, delete, RSVP) for the chat worker and the approval machine, and a profile sampler for first-learn.

Slugs and response shapes verified against the Composio API on 2026-09-13 (toolkit version pinned in
COMPOSIO_TOOLKIT_VERSIONS): FIND_EVENT answers with `data.event_data.event_data: [event resource]` next to the
calendar's own `defaultReminders` list, FIND_FREE_SLOTS with `data.calendars.primary.{busy,free}: [{start,end}]`.
The write actions' response shapes were not exercised against a live calendar; `_created` reads them defensively.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .documents import _data, _first

ExecuteFn = Callable[[str, dict[str, Any], str | None], dict[str, Any]]

CALENDAR = "primary"
WRITE_OPS = ("create", "update", "delete", "rsvp")
RSVP_RESPONSES = ("accepted", "declined", "tentative")
DEFAULT_DURATION = timedelta(hours=1)


# ---------------- time helpers ----------------

def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - an unknown zone name must not lose the event
        return ZoneInfo("UTC")


def _aware(iso: str, tz: str | None) -> datetime:
    """ISO string -> aware datetime; a naive string is read in `tz`."""
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_zone(tz))


def local_naive(iso: str, tz: str | None) -> str:
    """CREATE_EVENT wants wall-clock time without offset plus a separate IANA zone."""
    return _aware(iso, tz).astimezone(_zone(tz)).strftime("%Y-%m-%dT%H:%M:%S")


def rfc3339(iso: str, tz: str | None) -> str:
    return _aware(iso, tz).isoformat()


def default_end(start_iso: str, tz: str | None) -> str:
    return (_aware(start_iso, tz) + DEFAULT_DURATION).isoformat()


# ---------------- read ----------------

def _when(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    return str(part.get("dateTime") or part.get("date") or "")


def compact_event(e: dict[str, Any]) -> dict[str, Any]:
    """The event as the agent sees it: ids, times, who is invited and whether they answered. No raw resource."""
    start, end = e.get("start") or {}, e.get("end") or {}
    people = [a for a in (e.get("attendees") or []) if isinstance(a, dict) and a.get("email")]
    me = next((a for a in people if a.get("self")), None)
    organizer = e.get("organizer") or {}
    return {
        "id": _first(e, "id"),
        "title": _first(e, "summary", default="(no title)"),
        "start": _when(start),
        "end": _when(end),
        "all_day": bool(isinstance(start, dict) and start.get("date") and not start.get("dateTime")),
        "location": _first(e, "location"),
        "organizer": _first(organizer, "email") if isinstance(organizer, dict) else "",
        "organized_by_me": bool(isinstance(organizer, dict) and organizer.get("self")),
        "guests": [{"email": a["email"], "name": a.get("displayName") or "", "response": a.get("responseStatus") or ""}
                   for a in people if not a.get("self")],
        "my_response": (me or {}).get("responseStatus") or "",
        "status": _first(e, "status"),
        "url": _first(e, "htmlLink"),
        "meet": _first(e, "hangoutLink"),
        "description": _first(e, "description")[:300],
    }


def _events_of(data: Any) -> list[dict[str, Any]]:
    """Walk data.event_data.event_data (or data.items / data.events). Never "the first list of dicts": the calendar
    resource around the events also carries defaultReminders, which would read as events."""
    node = data
    for _ in range(3):
        if isinstance(node, list):
            return [x for x in node if isinstance(x, dict)]
        if not isinstance(node, dict):
            return []
        node = node.get("event_data", node.get("items", node.get("events")))
    return []


def _find_args(time_min: str, time_max: str, query: str, tz: str | None, limit: int) -> dict[str, Any]:
    args: dict[str, Any] = {"calendar_id": CALENDAR, "time_min": rfc3339(time_min, tz), "time_max": rfc3339(time_max, tz),
                            "single_events": True, "order_by": "startTime", "max_results": limit}
    if query.strip():
        args["query"] = query.strip()
    return args


def list_events(execute: ExecuteFn, uid: str | None, time_min: str, time_max: str, query: str = "", *,
                tz: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Events overlapping [time_min, time_max] on the primary calendar, recurring ones expanded, cancelled ones out."""
    data = _data(execute("GOOGLECALENDAR_FIND_EVENT", _find_args(time_min, time_max, query, tz, limit), uid))
    return [compact_event(e) for e in _events_of(data) if e.get("status") != "cancelled"][:limit]


def find_free_slots(execute: ExecuteFn, uid: str | None, time_min: str, time_max: str, tz: str | None) -> dict[str, Any]:
    """{"free": [{start,end}], "busy": [{start,end}]} for the primary calendar in the window."""
    data = _data(execute("GOOGLECALENDAR_FIND_FREE_SLOTS", {"items": [CALENDAR], "time_min": rfc3339(time_min, tz),
                                                             "time_max": rfc3339(time_max, tz), "timezone": tz or "UTC"}, uid))
    cals = data.get("calendars") if isinstance(data, dict) else None
    entry: Any = {}
    if isinstance(cals, dict):
        entry = cals.get(CALENDAR) or next((v for v in cals.values() if isinstance(v, dict)), {})
    elif isinstance(data, dict):
        entry = data
    return {"free": [s for s in (entry.get("free") or []) if isinstance(s, dict)],
            "busy": [s for s in (entry.get("busy") or []) if isinstance(s, dict)]}


# ---------------- write ----------------

def _created(data: Any, fallback_id: str = "") -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"id": fallback_id, "url": ""}
    inner = data.get("response_data") or data.get("event") or data.get("event_data") or {}
    src = inner if isinstance(inner, dict) and inner.get("id") else data
    return {"id": _first(src, "id", default=fallback_id), "url": _first(src, "htmlLink")}


def create_event(execute: ExecuteFn, uid: str | None, ev: dict[str, Any]) -> dict[str, Any]:
    tz = ev.get("timezone") or "UTC"
    start, end = str(ev["start"]), str(ev.get("end") or default_end(str(ev["start"]), tz))
    guests = [g for g in (ev.get("attendees") or []) if g]
    args: dict[str, Any] = {"calendar_id": CALENDAR, "summary": ev.get("title") or "(no title)",
                            "start_datetime": local_naive(start, tz), "end_datetime": local_naive(end, tz), "timezone": tz,
                            "create_meeting_room": bool(ev.get("meet")),      # Composio's default is True: no surprise Meet links
                            "send_updates": "all" if guests else "none"}
    if guests:
        args["attendees"] = guests
    for k in ("location", "description"):
        if ev.get(k):
            args[k] = ev[k]
    return _created(_data(execute("GOOGLECALENDAR_CREATE_EVENT", args, uid)))


def update_event(execute: ExecuteFn, uid: str | None, ev: dict[str, Any]) -> dict[str, Any]:
    """Only the fields given change; `attendees` replaces the guest list when given."""
    tz = ev.get("timezone") or "UTC"
    args: dict[str, Any] = {"calendar_id": CALENDAR, "event_id": str(ev["event_id"]), "send_updates": "all"}
    if ev.get("title"):
        args["summary"] = ev["title"]
    if ev.get("start"):
        args["start_time"] = rfc3339(str(ev["start"]), tz)
        args["end_time"] = rfc3339(str(ev.get("end") or default_end(str(ev["start"]), tz)), tz)
        args["timezone"] = tz
    elif ev.get("end"):
        args["end_time"] = rfc3339(str(ev["end"]), tz)
        args["timezone"] = tz
    for k in ("location", "description"):
        if ev.get(k):
            args[k] = ev[k]
    if ev.get("attendees") is not None and ev.get("attendees_given"):
        args["attendees"] = [g for g in ev["attendees"] if g]
    return _created(_data(execute("GOOGLECALENDAR_PATCH_EVENT", args, uid)), fallback_id=str(ev["event_id"]))


def delete_event(execute: ExecuteFn, uid: str | None, ev: dict[str, Any]) -> dict[str, Any]:
    _data(execute("GOOGLECALENDAR_DELETE_EVENT", {"calendar_id": CALENDAR, "event_id": str(ev["event_id"]), "send_updates": "all"}, uid))
    return {"id": str(ev["event_id"]), "url": ""}


def rsvp_event(execute: ExecuteFn, uid: str | None, ev: dict[str, Any]) -> dict[str, Any]:
    response = str(ev.get("response") or "")
    if response not in RSVP_RESPONSES:
        raise ValueError(f"rsvp response must be one of {RSVP_RESPONSES}")
    data = _data(execute("GOOGLECALENDAR_PATCH_EVENT", {"calendar_id": CALENDAR, "event_id": str(ev["event_id"]),
                                                         "rsvp_response": response, "send_updates": "all"}, uid))
    return _created(data, fallback_id=str(ev["event_id"]))


WRITERS = {"create": create_event, "update": update_event, "delete": delete_event, "rsvp": rsvp_event}


def write(execute: ExecuteFn, uid: str | None, ev: dict[str, Any]) -> dict[str, Any]:
    """Apply one calendar write ({"op": create|update|delete|rsvp, ...}) -> {"id", "url"}."""
    op = str(ev.get("op") or "")
    if op not in WRITERS:
        raise ValueError(f"unknown calendar op {op!r}")
    return WRITERS[op](execute, uid, ev)


# ---------------- profile sampler ----------------

def sample_googlecalendar(execute: ExecuteFn, composio_user_id: str, *, days_back: int = 30, days_ahead: int = 30) -> list[dict[str, Any]]:
    """Last and next month of events: whom the person meets and how often, plus the events themselves (titles only)."""
    now = datetime.now(tz=UTC)
    events = list_events(execute, composio_user_id, (now - timedelta(days=days_back)).isoformat(),
                         (now + timedelta(days=days_ahead)).isoformat(), limit=80)
    facts: list[dict[str, Any]] = []
    seen_self = False
    people: Counter[str] = Counter()
    names: dict[str, str] = {}
    for e in events:
        if e["organized_by_me"] and e["organizer"] and not seen_self:
            facts.append({"kind": "self", "email": e["organizer"]})
            seen_self = True
        for g in e["guests"]:
            people[g["email"]] += 1
            if g["name"]:
                names[g["email"]] = g["name"]
        facts.append({"kind": "event", "title": e["title"][:120], "when": e["start"][:16], "guests": len(e["guests"]),
                      "mine": e["organized_by_me"]})
    for email, count in people.most_common(24):
        facts.append({"kind": "contact", "name": names.get(email, ""), "email": email, "direction": "meets", "count": count})
    return facts


# ---------------- change news: baseline state, owner, review ----------------

STALE_CHANGE = timedelta(minutes=30)      # older changes are replays (the first poll after enabling) or late: not news
DETAIL_FIELDS = ("summary", "location")   # besides the time, what makes an organizer's edit worth telling
ANSWERS = ("accepted", "declined", "tentative")
_CALENDAR_MAIL = re.compile(r"^[^:@]{1,40}:\s*(?P<title>.+?)\s+@\s+.+\((?P<addr>[^()\s]+@[^()\s]+)\)\s*$")


def norm_title(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def calendar_mail_title(subject: str) -> str:
    """The event title inside a Google Calendar notification mail's subject, in any language:
    'Invitation: Weekly sync @ Mon 14 Sep 2026 12pm - 1pm (CEST) (me@example.com)', 'Güncellenmiş davet: …',
    'Accepted: …', 'Canceled event: …'. Empty for every other subject."""
    m = _CALENDAR_MAIL.match(str(subject or "").strip())
    return norm_title(m.group("title")) if m else ""


def event_title_of(payload: dict[str, Any]) -> str:
    """The calendar event an observation is news about: set by review_change for calendar changes, read from the
    subject for Google's own calendar mails. Empty when the observation is about no calendar event."""
    return norm_title(payload.get("event_title")) or calendar_mail_title(str(payload.get("subject") or ""))


def sync_shape(e: dict[str, Any]) -> dict[str, Any]:
    """A Google event resource in the EVENT_SYNC trigger's payload shape: the state review_change compares with."""
    organizer = e.get("organizer") if isinstance(e.get("organizer"), dict) else {}
    return {"summary": _first(e, "summary"), "start_time": _when(e.get("start")), "end_time": _when(e.get("end")),
            "status": _first(e, "status"), "location": _first(e, "location"),
            "organizer_email": _first(organizer, "email").lower(), "organizer_name": _first(organizer, "displayName"),
            "organizer_self": bool(organizer.get("self")),
            "attendees": [a for a in e.get("attendees") or [] if isinstance(a, dict)],
            "updated_at": _first(e, "updated"), "html_link": _first(e, "htmlLink")}


def snapshot_events(execute: ExecuteFn, uid: str | None, time_min: str, time_max: str, *, limit: int = 250) -> list[dict[str, Any]]:
    """Every event in the window as its current state (sync_shape plus its id); cancelled ones out."""
    data = _data(execute("GOOGLECALENDAR_FIND_EVENT", _find_args(time_min, time_max, "", None, limit), uid))
    return [{"id": _first(e, "id"), **sync_shape(e)} for e in _events_of(data) if e.get("status") != "cancelled" and e.get("id")]


def calendar_owner(execute: ExecuteFn, uid: str | None) -> str:
    """The primary calendar's id, which is its owner's address; empty when the answer carries none.
    GET_CALENDAR answers with data.calendar_data (verified 2026-09-14); the other keys are fallbacks."""
    data = _data(execute("GOOGLECALENDAR_GET_CALENDAR", {"calendar_id": CALENDAR}, uid))
    nodes = [data] + ([data.get(k) for k in ("calendar_data", "calendar", "response_data", "data")] if isinstance(data, dict) else [])
    for node in nodes:
        if isinstance(node, dict) and "@" in str(node.get("id") or ""):
            return str(node["id"]).strip().lower()
    return ""


def _people(value: Any) -> dict[str, dict[str, Any]]:
    """email -> {"status", "self", "name"} from Google attendee resources (or bare addresses)."""
    out: dict[str, dict[str, Any]] = {}
    for a in value or []:
        if isinstance(a, str) and "@" in a:
            out[a.strip().lower()] = {"status": "", "self": False, "name": ""}
        elif isinstance(a, dict) and a.get("email"):
            out[str(a["email"]).strip().lower()] = {
                "status": str(a.get("responseStatus") or a.get("response_status") or a.get("status") or "").lower(),
                "self": bool(a.get("self")), "name": str(a.get("displayName") or a.get("display_name") or a.get("name") or "")}
    return out


def _moment(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _addr(name: str, email: str) -> str:
    return f"{name} <{email}>" if name else email


def review_change(cur: dict[str, Any], prev: dict[str, Any] | None, own: set[str], now: datetime) -> dict[str, Any] | None:
    """Is this calendar change news for the person, and what is it?

    None means not news: a replay or a late change, the person's own edit (or Peyk's on their behalf), their own
    answer to an invitation, an edit nobody needs to hear about (guest list, description), or an update to an event
    never seen before (nothing to compare with). Otherwise the fields to merge into the observation: `change`
    (invited | moved | changed | cancelled | guest_answered), who caused it as `from`, the old values, and the
    normalized `event_title` for the "already told" check."""
    updated = _moment(cur.get("updated_at"))
    if updated is None or now - updated > STALE_CHANGE:
        return None
    prev = prev or {}
    people, before = _people(cur.get("attendees")), _people(prev.get("attendees"))
    own = {e.strip().lower() for e in own if e} | {e for e, a in {**before, **people}.items() if a["self"]}
    organizer = str(cur.get("organizer_email") or prev.get("organizer_email") or "").strip().lower()
    if not organizer:
        return None                                  # whose event is this? cannot tell, stay quiet
    mine = organizer in own or bool(cur.get("organizer_self") or prev.get("organizer_self"))
    sender = _addr(str(cur.get("organizer_name") or prev.get("organizer_name") or ""), organizer)
    context = {k: prev[k] for k in ("summary", "start_time", "end_time", "location", "organizer_name") if prev.get(k) and not cur.get(k)}
    base = {**context, "event_title": norm_title(cur.get("summary") or prev.get("summary"))}
    change_type = str(cur.get("change_type") or "").lower()
    if change_type == "deleted" or str(cur.get("status") or "").lower() == "cancelled":
        me = next((a for e, a in {**before, **people}.items() if e in own), None)
        if mine or (me and me["status"] == "declined"):
            return None                              # they deleted it themselves, or removed an invitation they declined
        return {**base, "change": "cancelled", "from": sender}
    if change_type == "created":
        return None if mine else {**base, "change": "invited", "from": sender}
    if not prev:
        return None                                  # an update to an event never seen: nothing to compare with
    if mine:                                         # the person's own event: only a guest's answer is news
        for email, a in people.items():
            if email not in own and a["status"] in ANSWERS and a["status"] != (before.get(email) or {}).get("status"):
                return {**base, "change": "guest_answered", "from": _addr(a["name"], email), "answer": a["status"],
                        **({"guest": a["name"]} if a["name"] else {})}
        return None
    start, end = _moment(cur.get("start_time")), _moment(cur.get("end_time"))
    was_start, was_end = _moment(prev.get("start_time")), _moment(prev.get("end_time"))
    if (start and was_start and start != was_start) or (end and was_end and end != was_end):
        return {**base, "change": "moved", "from": sender, "was_start": prev.get("start_time") or "", "was_end": prev.get("end_time") or ""}
    changed = [f for f in DETAIL_FIELDS if cur.get(f) and norm_title(cur.get(f)) != norm_title(prev.get(f))]
    if changed:
        return {**base, "change": "changed", "from": sender, **{f"was_{f}": prev.get(f) or "" for f in changed}}
    return None
