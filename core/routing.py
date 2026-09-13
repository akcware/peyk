"""Data-driven routing helpers. Workers call these instead of branching on `source`."""
from __future__ import annotations

from core.config import Settings
from core.models import ControlEvent, Observation

CONTROL_SOURCES = ("telegram", "whatsapp")
MAX_QUOTE_CHARS = 600


def is_control_channel(obs: Observation, settings: Settings) -> bool:
    """True for observations coming from a user's own control channel (Telegram, WhatsApp): commands, chat and
    button taps. Everything else is a world event to triage."""
    return obs.source in CONTROL_SOURCES or obs.source == settings.CONTROL_SOURCE


def control_event(obs: Observation) -> ControlEvent:
    """Normalize a control-channel observation. Adapters may pre-normalize into payload['control'];
    otherwise we understand the Telegram update shape."""
    if isinstance(obs.payload.get("control"), dict):
        return ControlEvent(**obs.payload["control"])
    cq = obs.payload.get("callback_query")
    if cq:
        frm = cq.get("from") or {}
        return ControlEvent(callback=cq.get("data"), callback_id=cq.get("id"),
                            message_id=(cq.get("message") or {}).get("message_id"),
                            display_name=" ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or None)
    msg = obs.payload.get("message") or {}
    frm = msg.get("from") or {}
    return ControlEvent(text=msg.get("text"), message_id=msg.get("message_id"),
                        display_name=" ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or None,
                        reply_to=reply_context(msg))


def reply_context(msg: dict) -> str | None:
    """The message the person tapped "reply" on (Telegram `reply_to_message`), as one bracketed line the agent
    reads before their text — "bunun hakkında" then refers to that message, not to the last topic. Kept apart
    from `text` so command parsing and draft edits see the person's words only."""
    q = msg.get("reply_to_message")
    if not isinstance(q, dict):
        return None
    quoted = q.get("text") or q.get("caption") or ""
    if not quoted:
        quoted = "(a photo)" if q.get("photo") else "(a voice message)" if q.get("voice") else "(a message without text)"
    quoted = " ".join(str(quoted).split())
    if len(quoted) > MAX_QUOTE_CHARS:
        quoted = quoted[:MAX_QUOTE_CHARS].rstrip() + "…"
    who = "your message" if (q.get("from") or {}).get("is_bot") else "their own earlier message"
    return f'[replying to {who}: "{quoted}"]'


def agent_text(obs: Observation) -> str:
    """What the chat agent reads for a control message: the reply context line (if any) and the text."""
    ev = control_event(obs)
    return "\n".join(p for p in (ev.reply_to, ev.text) if p)


def is_callback(obs: Observation) -> bool:
    return control_event(obs).callback is not None


def callback_data(obs: Observation) -> str | None:
    return control_event(obs).callback


def control_text(obs: Observation) -> str | None:
    return control_event(obs).text
