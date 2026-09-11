"""First-learn: turn a metadata sample of a freshly connected service into a few facts the person can confirm.
Runs once per connected service. Sonnet-class model, structured output. No DB access here."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from strands import Agent

from agent.model import build_model, user_language, user_profile
from agent.schemas import LearnResult

LEARN_SYSTEM_PROMPT = """You are a personal assistant who just got access to one of the person's services and looks at the
metadata of their recent activity (senders, subjects, document titles — never bodies) to get to know them.

About the person so far:
{profile}

Write in {language}. Be honest and modest: you saw metadata, not content. Propose 3-6 durable facts worth
remembering (who they deal with most and in what role, recurring topics, what seems urgent for them, rhythms
like weekly reports). Skip anything sensitive or embarrassing (health, finances beyond invoices, private
relationships, religion, politics) and skip newsletters/marketing. Life situations that may be sensitive (job
search, housing/tenancy, money troubles) go into `message` only as a gentle question, never into `facts` or
`profile_suggestion` — the person can add them when confirming. Keep `message` to at most 4 sentences. Then write `message`: 2-5 sentences in the person's language,
first person, warm and brief — what you noticed, and a request to confirm or correct it ("doğru mu, düzeltmek
istediğin var mı?"). Do not list every fact mechanically; make it read like a colleague summarizing.
`profile_suggestion` is a compact third-person profile (who they are, what matters, what is urgent) that you
would keep if they confirm."""


def render_facts(service: str, facts: list[dict[str, Any]]) -> str:
    lines = [f"service: {service}", f"sample size: {len(facts)} items", ""]
    contacts = [f for f in facts if f.get("kind") == "contact"]
    subjects = [f for f in facts if f.get("kind") == "subject"]
    docs = [f for f in facts if f.get("kind") in ("document", "workspace", "database")]
    if contacts:
        lines.append("people (direction, count):")
        for c in contacts[:24]:
            lines.append(f"- {c.get('name') or c.get('email')} <{c.get('email','')}> {c.get('direction','')} x{c.get('count',1)}")
    if subjects:
        lines.append("")
        lines.append("recent subjects (from, date):")
        for s in subjects[:80]:
            lines.append(f"- {s.get('text')} — {s.get('from')} — {s.get('when')}")
    if docs:
        lines.append("")
        lines.append("documents / workspaces:")
        for d in docs[:40]:
            lines.append(f"- [{d.get('service', service)}] {d.get('title') or d.get('name')} ({d.get('updated', '')})")
    other = [f for f in facts if f.get("kind") not in ("contact", "subject", "document", "workspace", "database")]
    for o in other[:20]:
        lines.append(f"- {o}")
    return "\n".join(lines)


@lru_cache
def _model():
    return build_model("chat", temperature=0.2, max_tokens=1200)


def learn(payload: dict[str, Any]) -> dict[str, Any]:
    """payload: {"service": str, "facts": [...], "user": {...}} -> LearnResult dict."""
    user = payload.get("user") or {}
    agent = Agent(
        model=_model(),
        system_prompt=LEARN_SYSTEM_PROMPT.format(profile=user_profile(payload), language=user_language(user.get("language"))),
        callback_handler=None,
    )
    prompt = render_facts(str(payload.get("service") or "service"), payload.get("facts") or [])
    result = agent(prompt + "\n\nReturn the learn result.", structured_output_model=LearnResult)
    out: LearnResult = result.structured_output
    return out.model_dump()
