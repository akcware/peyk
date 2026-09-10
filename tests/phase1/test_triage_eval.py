"""Real-model eval (pytest -m llm). |model - label| <= 1 for >= 80%; recall on label 5 == 1.0."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.llm

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "labeled_triage.jsonl"


def _load_env_from_settings() -> None:
    """agent/ reads os.environ only (no core.config import by design); tests bridge .env for it."""
    from core.config import export_agent_env, get_settings

    export_agent_env(get_settings())


def test_triage_agent_eval():
    _load_env_from_settings()
    from agent.triage_agent import triage

    rows = [json.loads(line) for line in FIX.read_text().splitlines() if line.strip()]
    close, fives_hit, fives = 0, 0, 0
    misses = []
    for r in rows:
        obs = {"source": "gmail", "kind": "message_in", "occurred_at": "2026-09-10T09:00:00Z",
               "payload": {"from": r["from"], "subject": r["subject"], "snippet": r["snippet"]}}
        res, _meta = triage(obs, {"prior_messages_from_sender": 0})
        diff = abs(res.urgency - r["label_urgency"])
        close += diff <= 1
        if r["label_urgency"] == 5:
            fives += 1
            fives_hit += res.urgency >= 4
        if diff > 1:
            misses.append((r["subject"], r["label_urgency"], res.urgency, res.reason))
    print("\nmisses:", *misses, sep="\n  ")
    assert close / len(rows) >= 0.8, f"within-1 accuracy {close}/{len(rows)}"
    assert fives_hit == fives, f"label-5 recall {fives_hit}/{fives}"
