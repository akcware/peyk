# Engineering notes: phase gates and measurements

Moved out of the README; kept as the project's test record.

## Gates

Each phase closes with `make gate-N`; the next phase's branch starts from tag `phase-N`.

```bash
make gate-0     # tests/phase0 against TEST_DATABASE_URL (compose db, database agent_test)
```

### Phase 0 — manual checklist

Record results here before tagging `phase-0`.

- [x] Send yourself a mail → Composio trigger log shows it → raw notification in Telegram. Measured latency (2026-09-10, trigger interval 1 min): mail sent 16:28:20Z → observation 16:29:08Z → Telegram 16:29:09Z = **49 s**; earlier two mails: 11 s and 38 s. Bounded by Composio's polling interval, not by us.
- [x] Disable the trigger in Composio → send a mail → re-enable. Outcome: **event never arrived** (trigger disabled 16:30:16Z, mail sent while disabled, re-enabled 16:33:27Z, nothing within 4 min; Composio's poller restarts from "now" on enable and does not replay). Phase 2's `reconcile` job (`GMAIL_FETCH_EMAILS` since last cursor, `ON CONFLICT DO NOTHING`) exists exactly for this.
- [x] Workers stopped (Ctrl+C 16:38:07Z) → mail sent 16:38:20Z → workers restarted 16:38:26Z → observation 16:39:26Z, notified. **No loss for a short outage** because Composio's poll fired after the restart. Caveat: an outage that spans a poll tick (≥ 1 min) almost certainly loses the pushed event (websocket delivery has no replay; see test 2). Phase 2 `reconcile` closes that gap; re-run this test with a ≥ 2 min outage after phase 2 to confirm.

### Phase 1 — triage eval (real model)

`make gate-1-llm` on 2026-09-10, Bedrock `us.anthropic.claude-haiku-4-5-20251001-v1:0`, 60 synthetic labeled mails
(`tests/fixtures/labeled_triage.jsonl`): within-1 accuracy **59/60 (98%)**, label-5 recall **6/6 (100%)**, 85 s wall clock.
Only miss: "Appointment confirmation" rated 4 vs label 2 (model read "today at 10:00" as time-critical).
