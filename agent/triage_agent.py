"""Triage: "is this important?" — decided by the model. "Is it worth interrupting?" is NOT decided here
(workers/gate.py). Quota, mutes and cooldowns never enter this prompt on purpose.

The prompt is source-agnostic: it renders the observation payload as a key/value list, so a calendar
event or a WhatsApp message goes through the same template (phase 5 gate)."""
from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any

from strands import Agent

from agent.model import build_model, model_id, user_language
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
- reason: one short internal sentence (max 200 characters) explaining the urgency.
- summary: what a good personal assistant would say to the person about this, 1-2 sentences, max 320 characters.
  The summary MUST be written in {language} — the person's language — even when the event text is in another
  language. Name who it is from, what it is about, what (if anything) the person must do and by when.
  Do not paste the text; paraphrase. Example (English): Mara (client) reminds you invoice #2041 is due Friday; no reply needed."""


def render_observation(payload: dict[str, Any], *, max_text: int = 1200) -> str:
    """Generic key/value rendering. No per-source template — see phase 5."""
    lines: list[str] = []
    for key, value in payload.items():
        if value in (None, "", [], {}):
            continue
        text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        text = text.replace("\r", "").strip()
        if len(text) > max_text:
            text = text[:max_text] + " …[truncated]"
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def render_prompt(observation: dict[str, Any], sender_context: dict[str, Any] | None) -> str:
    src = observation.get("source", "unknown")
    kind = observation.get("kind", "event")
    occurred = observation.get("occurred_at", "")
    ctx = sender_context or {}
    ctx_lines = [f"{k}: {v}" for k, v in ctx.items() if v not in (None, "", [])]
    parts = [
        f"source: {src}",
        f"kind: {kind}",
        f"occurred_at: {occurred}",
        "",
        "--- event ---",
        render_observation(observation.get("payload") or {}),
    ]
    if ctx_lines:
        parts += ["", "--- sender context ---", *ctx_lines]
    parts += ["", "Return the triage result."]
    return "\n".join(parts)


@lru_cache
def _agent() -> Agent:
    profile = os.environ.get("USER_PROFILE", "(no profile provided)")
    language = user_language()
    return Agent(
        model=build_model("triage", temperature=0.0, max_tokens=512),
        system_prompt=TRIAGE_SYSTEM_PROMPT.format(profile=profile, language=language),
        callback_handler=None,
    )


def triage(observation: dict[str, Any], sender_context: dict[str, Any] | None = None) -> tuple[TriageResult, dict[str, Any]]:
    """Returns (result, meta) where meta has model_id and latency_ms. Stateless: a fresh message list per call."""
    agent = _agent()
    agent.messages = []  # never carry history between observations
    t0 = time.perf_counter()
    result = agent(render_prompt(observation, sender_context), structured_output_model=TriageResult)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    out: TriageResult = result.structured_output
    return out, {"model_id": model_id("triage"), "latency_ms": latency_ms}
