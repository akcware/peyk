"""AgentCore payload contract + static architecture checks (no LLM)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agent import app as agent_app
from agent.schemas import TriageResult
from agent.triage_agent import render_prompt

ROOT = Path(__file__).resolve().parents[2]


def test_agentcore_contract_local():
    def fake_triage(observation, sender_context):
        assert observation["payload"]["subject"] == "x"
        assert sender_context == {"prior_messages_from_sender": 2}
        return TriageResult(urgency=3, category="person", reason="ok"), {"model_id": "m", "latency_ms": 5}

    out = agent_app.handle({"task": "triage", "observation": {"source": "gmail", "kind": "message_in", "payload": {"subject": "x"}},
                            "sender_context": {"prior_messages_from_sender": 2}}, triage_fn=fake_triage)
    assert out == {"task": "triage", "result": {"urgency": 3, "category": "person", "reason": "ok"}, "model_id": "m", "latency_ms": 5}
    assert TriageResult(**out["result"]).urgency == 3
    assert "error" in agent_app.handle({"task": "nope"})


def test_prompt_is_generic_and_omits_budget():
    p = render_prompt({"source": "calendar", "kind": "event_starting", "occurred_at": "t",
                       "payload": {"summary": "Standup", "attendees": ["a@x.test", "b@x.test"], "empty": None}},
                      {"prior_messages_from_sender": 3})
    assert "summary: Standup" in p and "attendees: a@x.test, b@x.test" in p and "empty" not in p
    for banned in ("quota", "mute", "cooldown", "quiet"):
        assert banned not in p.lower()


def _grep(pattern: str, paths: list[str]) -> list[str]:
    r = subprocess.run(["grep", "-rnE", pattern, *paths, "--include=*.py"], cwd=ROOT, capture_output=True, text=True, check=False)
    return [line for line in r.stdout.splitlines() if line]


def test_no_source_branches_in_workers():
    hits = [h for h in _grep(r"source (==|in) ", ["workers"]) if "#" not in h.split(":", 2)[2][:2]]
    assert hits == [], hits


def test_agent_never_touches_db():
    assert _grep(r"^(from|import) (psycopg|core\.db|core\.repo)", ["agent"]) == []


def test_composio_import_boundary():
    hits = _grep(r"^(from|import) composio", ["core", "workers", "gateway", "agent"])
    assert hits == [], hits


def test_prompt_has_no_source_specific_branch():
    src = (ROOT / "agent" / "triage_agent.py").read_text()
    assert not re.search(r"if .*source.*==", src)
