"""Data-driven routing helpers. Workers call these instead of branching on `source`."""
from __future__ import annotations

from core.config import Settings
from core.models import Observation


def is_control_channel(obs: Observation, settings: Settings) -> bool:
    """True for observations coming from the user's own control channel (Telegram by default):
    commands, chat and button callbacks. Everything else is a world event to triage."""
    return obs.source == settings.CONTROL_SOURCE


def is_callback(obs: Observation) -> bool:
    return "callback_query" in obs.payload


def callback_data(obs: Observation) -> str | None:
    cq = obs.payload.get("callback_query") or {}
    return cq.get("data")


def control_text(obs: Observation) -> str | None:
    msg = obs.payload.get("message") or {}
    return msg.get("text")
