# Testing and measurements

## The suite

128 tests. 126 run offline (`make test`, `pytest -m "not llm"`); two need a real model (`make gate-1-llm`).
Database tests use `TEST_DATABASE_URL` (the compose Postgres, database `agent_test`): the schema is rebuilt from
`db/migrations/` once per session and every table is truncated before each test. No test calls Telegram, Composio
or Bedrock; adapters and the agent are replaced by fakes at the same interfaces the workers use.

| Directory | Covers |
|---|---|
| `tests/phase0/` | queue claim/complete/recovery, dedup, Composio event → observation mapping, webhook signature, Telegram update parsing, identity normalisation |
| `tests/phase1/` | agent payload contract (and that `agent/` imports no DB code), gate rules, budget repo, full worker flow with a fake agent, a replay of 60 labelled mails, the triage eval (`llm`) |
| `tests/phase2/` | scheduler, recurrences, tick handlers (brief, reminder, reconcile) |
| `tests/phase3/` | chat: history, context building, rounds, intents, memory (`llm` for the embedding test) |
| `tests/phase4/` | approval state machine: hash on approve, edit invalidates old buttons, idempotent send |
| `tests/phase5/` | Calendar as a data-only source: mapping fixture, prompt rendering, and `git diff phase-4..phase-5` touching no core file |
| `tests/multiuser/` | user directory, per-user routing and notifiers, agent-driven onboarding, first-learn and confirmation, document search/read/create |

Invariants a test will break if you violate them:

- `agent/` never imports `psycopg`, `core.db` or `core.repo` (`tests/phase1/test_agent_contract.py`,
  `tests/phase3/test_chat.py`).
- A Send button with an old content hash is refused; an edited draft needs a new approval (`tests/phase4/`).
- The gate is a pure function; quota, cooldown, quiet hours, mutes and the bypass reserve are table-driven cases
  (`tests/phase1/test_gate_rules.py`).
- Adding a source changes only `adapters/composio/mappings.py`, scripts, fixtures, docs and tests
  (`tests/phase5/test_calendar.py::test_core_untouched`, compares the tagged commits).

## Phase gates

The project was built in phases, each closed by `make gate-N` and a git tag (`phase-0`, `phase-4`, `phase-5` are
on the repository; the intermediate phases were closed on the same branch). The gates still exist as make targets
and simply run the corresponding test directory; `make gate-multiuser` covers what came after phase 5.

## Measurements

Recorded during development; dates are when they were taken.

**Composio → Telegram latency** (2026-09-10, Gmail trigger interval 1 min). Mail sent 16:28:20Z → observation
16:29:08Z → Telegram 16:29:09Z: **49 s**. Two earlier mails: 11 s and 38 s. Bounded by Composio's polling
interval, not by Peyk.

**A disabled trigger loses events** (2026-09-10). Trigger disabled 16:30:16Z, a mail sent while disabled,
trigger re-enabled 16:33:27Z: nothing arrived within 4 minutes. Composio's poller restarts from "now" on enable
and does not replay. This is why the `reconcile` job exists (backfill since cursor − lookback, insert
`ON CONFLICT DO NOTHING`).

**Short worker outage** (2026-09-10). Workers stopped 16:38:07Z, mail sent 16:38:20Z, workers restarted
16:38:26Z, observation 16:39:26Z and notified. No loss, because Composio's next poll fired after the restart. An
outage spanning a poll tick would lose the pushed event; `reconcile` closes that gap.

**Triage eval** (2026-09-10, `make gate-1-llm`, Bedrock `us.anthropic.claude-haiku-4-5-20251001-v1:0`, 60 synthetic
labelled mails in `tests/fixtures/labeled_triage.jsonl`): within-1 accuracy **59/60 (98 %)**, urgency-5 recall
**6/6**, 85 s wall clock. The only miss: an appointment confirmation rated 4 against a label of 2, the model read
"today at 10:00" as time-critical.

**Budget replay** (`tests/phase1/test_replay.py`). The 60 labelled mails are replayed through the gate as 20 per
day over three days with a quota of 5. Every urgency-5 mail must get through, no day may exceed the quota, and the
total must stay well under a third of the input. The `bypass_reserve` setting (default 2) came out of an earlier
run of this replay in which morning urgency-3/4 mails used every slot before an afternoon urgency-5 arrived.
