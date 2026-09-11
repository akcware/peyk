"""Data-driven routing helpers. Workers call these instead of branching on `source`."""
from __future__ import annotations

from core.config import Settings
from core.models import ControlEvent, Observation

CONTROL_SOURCES = ("telegram", "whatsapp")


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
                        display_name=" ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or None)


def is_callback(obs: Observation) -> bool:
    return control_event(obs).callback is not None


def callback_data(obs: Observation) -> str | None:
    return control_event(obs).callback


def control_text(obs: Observation) -> str | None:
    return control_event(obs).text
