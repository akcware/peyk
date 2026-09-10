"""Phase 5 gate: a second source must be a data change only."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from adapters.composio.webhook import parse_envelope, to_observation
from agent.triage_agent import render_prompt
from core.repo import budget_repo, observation_repo
from tests.conftest import USER_ID
from tests.phase1.test_worker_flow import FakeTelegram, fake_agent
from workers import triage

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "composio_events" / "calendar_event_starting.json"

ALLOWED_PREFIXES = ("adapters/composio/mappings.py", "scripts/", "tests/phase5/", "tests/fixtures/", ".env.example", "README.md",
                    "Makefile", "docs/")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


def test_core_untouched():
    """Everything changed since tag phase-4 must be outside core/ workers/ gateway/ agent/ adapters/*/adapter.py."""
    if "phase-4" not in _git("tag").split():
        return  # baseline tag missing (fresh clone without tags): nothing to compare against
    changed = [line for line in _git("diff", "--name-only", "phase-4..HEAD").splitlines() if line]
    changed += [line for line in _git("diff", "--name-only", "HEAD").splitlines() if line]      # uncommitted too
    offenders = [f for f in changed if not f.startswith(ALLOWED_PREFIXES)]
    assert offenders == [], f"phase 5 touched core files: {offenders}"


def _event() -> dict:
    return json.loads(FIX.read_text())


def test_calendar_mapping_parses_fixture():
    obs = to_observation(parse_envelope(_event()), USER_ID)
    assert obs is not None
    assert obs.source == "calendar" and obs.kind == "event_starting"
    assert obs.source_key == "evt_7h2k9m4p:2026-09-11T09:00:00+02:00"
    assert obs.thread_key == "evt_7h2k9m4p"
    assert obs.occurred_at == datetime(2026, 9, 11, 7, 0, tzinfo=UTC)
    assert obs.payload["summary"] == "Standup with client team"
    assert obs.payload["attendees"] == ["you@example.test", "mara@example-client.test"]
    assert obs.payload["hangout_link"].startswith("https://meet.")
    assert obs.payload["minutes_until_start"] == 15
    assert "html_link" not in obs.payload           # only mapped fields


async def test_event_fires_once(conn):
    a = to_observation(parse_envelope(_event()), USER_ID)
    b = to_observation(parse_envelope(_event()), USER_ID)      # Composio re-fires for the same event/start
    assert await observation_repo.insert(conn, a) is not None
    assert await observation_repo.insert(conn, b) is None
    assert await observation_repo.count(conn, USER_ID, source="calendar") == 1
    moved = _event()
    moved["data"]["start_time"] = "2026-09-11T10:00:00+02:00"     # rescheduled -> a new event_starting
    assert await observation_repo.insert(conn, to_observation(parse_envelope(moved), USER_ID)) is not None
    assert await observation_repo.count(conn, USER_ID, source="calendar") == 2


async def test_calendar_flows_through_gate(conn, settings):
    obs = await observation_repo.insert(conn, to_observation(parse_envelope(_event()), USER_ID))
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)
    await triage.handle(obs, settings=settings, agent=fake_agent(5, "calendar"), notifier=notifier)
    assert (await budget_repo.get_triage(conn, obs.id))["category"] == "calendar"
    assert len(tg.sent) == 1 and "[calendar]" in tg.sent[0]["text"] and "Standup with client team" in tg.sent[0]["text"]
    # and a low-urgency calendar event stays silent, same gate
    quiet = _event(); quiet["data"]["event_id"] = "evt_other"
    obs2 = await observation_repo.insert(conn, to_observation(parse_envelope(quiet), USER_ID))
    await triage.handle(obs2, settings=settings, agent=fake_agent(2, "calendar"), notifier=notifier)
    assert len(tg.sent) == 1


def test_prompt_renders_calendar_without_source_branch():
    obs = to_observation(parse_envelope(_event()), USER_ID)
    p = render_prompt({"source": obs.source, "kind": obs.kind, "occurred_at": obs.occurred_at.isoformat(), "payload": obs.payload}, None)
    assert "source: calendar" in p and "kind: event_starting" in p and "summary: Standup with client team" in p
    assert "attendees: you@example.test, mara@example-client.test" in p


def test_no_source_branches_in_workers():
    r = subprocess.run(["grep", "-rnE", r"source (==|in) ", "workers", "--include=*.py"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert r.stdout.strip() == "", r.stdout
