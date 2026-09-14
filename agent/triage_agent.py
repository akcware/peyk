"""Triage: "is this important?" — decided by the model. "Is it worth interrupting?" is NOT decided here
(workers/gate.py). Quota, mutes and cooldowns never enter this prompt on purpose.

The prompt is source-agnostic: it renders the observation payload as a key/value list, so a calendar
event or a WhatsApp message goes through the same template (phase 5 gate)."""
from __future__ import annotations

import time
from datetime import datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from strands import Agent

from agent.model import build_model, model_id, retry_strategy, user_language, user_profile
from agent.schemas import TriageResult

TRIAGE_SYSTEM_PROMPT = """You triage incoming events for one person and rate how urgently they need to see each one.

About the person:
{profile}

Rate urgency 1-5:
1 = ignore (bulk mail, marketing, automated noise, receipts that need no action)
2 = low (FYI, can wait days; newsletters they actually read; routine updates)
3 = normal (a real person wants something, no deadline today; batch into a daily summary)
4 = high (needs a reply or action today; a client, a deadline, money, health, a meeting in a few hours)
5 = interrupt now (time-critical within the hour, safety, family emergency, a meeting starting, something is broken and they own it)

Categories: person (a human wrote it directly), transactional (receipt, confirmation, delivery),
newsletter, automated (system notification, alert, CI, monitoring), calendar (event/meeting), other.

Rules:
- Judge from the content and who sent it. A known frequent correspondent matters more than a stranger.
- Marketing that pretends to be urgent ("last chance!") is 1.
- Never rate above 3 unless there is a concrete reason in the text (deadline, question, money, meeting time).
- Rate 5 only when waiting an hour would have a real cost.
- What the person said matters to them wins over these defaults: when the profile names kinds of events as urgent
  or very important for them (e.g. meetings, bills, mail from a certain person), such an event that concerns them
  (not marketing about it) is 5.
- Timestamps in the event fields (start_time, was_start, ...) are already in the person's current time zone: say
  clock times exactly as they appear there.
- A calendar change carries `change`: invited (someone invited the person), moved (the organizer changed the
  time; was_start is the old start), changed (a new title or place), cancelled, guest_answered (a guest answered
  the person's own invitation; answer says how). A change to something today or tomorrow is 4, a later one 3; a
  guest's answer is 2, or 3 when it is a decline for a meeting within a day. Say what changed and when it is.
- proposed_start / proposed_end: when the event asks the person to meet, attend or be somewhere at a clock time
  (someone proposes a meeting or a call, an invitation, a meeting moved to a new time), give that start, and the end
  only when the text says it, as ISO-8601 with the offset of the person's time zone now, on the date the text means
  ("tomorrow" is the day after occurred_at on their clock). Leave both empty for anything else: a deadline, a past
  event, a delivery window, a meeting starting now (kind event_starting), marketing.
- A "calendar at that time" section means their calendar was just read for that time. Then the summary must also
  say, in a few words, whether they are free then: name what overlaps and its time, or what ends right before or
  starts right after it (back to back); if nothing is near, that they are free. Without that section never say
  anything about their calendar.
- reply: without that section reply is always empty. With it, and only when a person wrote to them asking or
  proposing to meet at that time: the answer the person could send back, first person, 1-3 short sentences, no
  signature, plain words, no em dashes (—). Write it in the language the message itself is written in, which may
  differ from {language} (the summary's language): a Turkish mail gets a Turkish reply. Free: accept. Taken: say
  that time does not work and offer one or two of the free alternatives. Leave it empty for calendar invitations.
- reason: one short internal sentence (max 200 characters) explaining the urgency.
- summary: what Peyk, the person's assistant — a quiet messenger who brings only what matters — would say to
  the person about this, 1-2 sentences, max 320 characters.
  The summary MUST be written in {language} — the person's language — even when the event text is in another
  language. Name who it is from, what it is about, what (if anything) the person must do and by when.
  Do not paste the text; paraphrase. Spoken, plain words: no em dashes (—), no bold, no ids.
  Example (English): Mara (client) reminds you invoice #2041 is due Friday; no reply needed."""


def _zone(name: Any) -> ZoneInfo | None:
    try:
        return ZoneInfo(str(name)) if name else None
    except (ValueError, KeyError):
        return None


def _in_zone(text: str, zone: ZoneInfo | None) -> str:
    """An ISO timestamp with an offset, shown on the person's clock; anything else unchanged."""
    if zone is None or len(text) <= 10 or "T" not in text:
        return text
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return text
    return dt.astimezone(zone).isoformat() if dt.tzinfo else text


def render_observation(payload: dict[str, Any], *, max_text: int = 1200, zone: ZoneInfo | None = None) -> str:
    """Generic key/value rendering. No per-source template — see phase 5. Timestamps are shown in `zone`."""
    lines: list[str] = []
    for key, value in payload.items():
        if value in (None, "", [], {}):
            continue
        text = ", ".join(map(str, value)) if isinstance(value, list) else _in_zone(str(value), zone)
        text = text.replace("\r", "").strip()
        if len(text) > max_text:
            text = text[:max_text] + " …[truncated]"
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


DERIVED_KEYS = ("calendar_check", "suggested_reply")   # what an earlier triage added to the payload; not the event


def _clock(start: str, end: str = "") -> str:
    """'Tue 15 Sep 13:00-14:00' from ISO times that are already on the person's clock."""
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end) if end else None
    except ValueError:
        return start
    if e is None:
        return f"{s:%a %d %b %H:%M}"
    return f"{s:%a %d %b %H:%M}-{e:%H:%M}" if e.date() == s.date() else f"{s:%a %d %b %H:%M} - {e:%a %d %b %H:%M}"


def render_calendar_check(check: dict[str, Any]) -> list[str]:
    """calendar.check_time's answer as prompt lines."""
    asked = check.get("asked") or {}
    lines = [f"asked time: {_clock(str(asked.get('start') or ''), str(asked.get('end') or ''))}",
             f"free then: {'yes' if check.get('free') else 'no'}"]
    for key, label in (("overlaps", "overlaps with"), ("right_before", "ends right before it"), ("right_after", "starts right after it")):
        items = check.get(key) or []
        if items:
            lines.append(f"{label}: " + "; ".join(f"{e.get('title')} ({_clock(str(e.get('start')), str(e.get('end')))})" for e in items))
    alternatives = check.get("alternatives") or []
    if alternatives:
        lines.append("free alternatives that day: " + ", ".join(_clock(str(a.get("start")), str(a.get("end"))) for a in alternatives))
    return lines


def render_prompt(observation: dict[str, Any], sender_context: dict[str, Any] | None) -> str:
    src = observation.get("source", "unknown")
    kind = observation.get("kind", "event")
    occurred = observation.get("occurred_at", "")
    ctx = sender_context or {}
    ctx_lines = [f"{k}: {v}" for k, v in ctx.items() if v not in (None, "", [])]
    tz = str((observation.get("user") or {}).get("timezone") or "")
    zone = _zone(tz)
    parts = [f"source: {src}", f"kind: {kind}", f"occurred_at: {_in_zone(str(occurred), zone)}"]
    if zone is not None:
        parts.append(f"the person's time zone now: {tz} (every timestamp below is shown in it)")
    payload = {k: v for k, v in (observation.get("payload") or {}).items() if k not in DERIVED_KEYS}
    parts += ["", "--- event ---", render_observation(payload, zone=zone)]
    if ctx_lines:
        parts += ["", "--- sender context ---", *ctx_lines]
    if observation.get("calendar_check"):
        parts += ["", "--- calendar at that time (read just now, times on the person's clock) ---",
                  *render_calendar_check(observation["calendar_check"]),
                  ("(if a person asks to meet: write reply in the language of the event text above, which can differ "
                   "from the summary's language)")]
    parts += ["", "Return the triage result."]
    return "\n".join(parts)


@lru_cache
def _model():
    return build_model("triage", temperature=0.0, max_tokens=1024)   # room for a summary and a reply draft


def _agent(observation: dict[str, Any]) -> Agent:
    user = observation.get("user") or {}
    return Agent(
        model=_model(),
        system_prompt=TRIAGE_SYSTEM_PROMPT.format(profile=user_profile(observation), language=user_language(user.get("language"))),
        callback_handler=None,
        retry_strategy=retry_strategy(),
    )


def triage(observation: dict[str, Any], sender_context: dict[str, Any] | None = None) -> tuple[TriageResult, dict[str, Any]]:
    """Returns (result, meta) where meta has model_id and latency_ms. Stateless: a fresh agent per call."""
    agent = _agent(observation)
    t0 = time.perf_counter()
    result = agent(render_prompt(observation, sender_context), structured_output_model=TriageResult)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    out: TriageResult = result.structured_output
    return out, {"model_id": model_id("triage"), "latency_ms": latency_ms}
