# Proactive Agent

A personal agent that *watches* your channels (Gmail, Calendar, Telegram) and interrupts you only when it matters.
Built for the [Agents for Humans](https://agentsforhumans.devpost.com/) hackathon with Strands Agents on Amazon Bedrock.

> Status: phases 0–4 implemented and unit-tested (`make gate-0` … `gate-4`). Phase-0 manual tests recorded below; phase 1–4 manual tests are pending Bedrock model access (Anthropic use-case form).

## How it works (phase 0)

```
Composio trigger (Gmail)  ──ws / webhook──▶  observation table  ──claim_next()──▶  notify worker ──▶ Telegram
Telegram getUpdates       ──long-poll─────▶  (Postgres queue: FOR UPDATE SKIP LOCKED, dedup by unique key)
```

- `core/` — config, DB pool, models, `SourceAdapter` protocol + registry, identity normalization, all SQL (`core/repo`), the queue.
- `adapters/composio/` — the only place the `composio` SDK is imported. `mappings.py` turns trigger payloads into observations declaratively.
- `adapters/telegram/` — Bot API over `httpx`, four endpoints, no library.
- `gateway/` — FastAPI: `/health`, `POST /webhook/composio` (HMAC verify → insert → 200).
- `workers/` — long-lived asyncio loops: ingest (per adapter), notify, stale-claim recovery.
- `agent/` — (phase 1) the only package deployed to AgentCore. It never touches the DB.

## Quickstart

```bash
cp .env.example .env            # fill TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, COMPOSIO_API_KEY, COMPOSIO_USER_ID
uv sync --group dev
make migrate                    # starts postgres (compose, port 5433) and applies db/migrations
uv run python scripts/composio_setup.py --toolkit gmail    # OAuth link + enables GMAIL_NEW_GMAIL_MESSAGE
make run-workers                # terminal 1
make run-gateway                # terminal 2 (only needed for webhook delivery)
```

Delivery mode: `COMPOSIO_DELIVERY=ws` (local, no public URL) or `webhook` (set the URL with
`scripts/composio_setup.py --webhook https://<host>/webhook/composio` and put the dashboard secret in `COMPOSIO_WEBHOOK_SECRET`).

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

## Multi-user and onboarding

Anyone can message the bot. The first message from an unknown chat creates an `app_user` row; the chat agent
greets, asks who they are (saved via `set_profile`), and offers to connect Gmail / Google Calendar. When the
person agrees, the agent's `connect_service` intent makes the worker create a Composio OAuth link, send it, and
poll (`await_connection` job, every 20 s, 15 min limit) until the account is ACTIVE — then the triggers for that
person are enabled and the agent confirms. No slash commands; the person can ask to connect anything later.

**First-learn.** Right after a service is connected, a detached job samples the last 30 days of metadata
(senders, subjects, document titles — never bodies), registers the people in it, and lets the agent propose
3–6 facts plus a profile: "I looked at senders and subjects, not content. You mostly deal with X about Y —
correct me?" Nothing is stored until the person confirms or corrects it (`confirm_learned`). Sensitive life
situations are only ever asked about, never inferred into memory.

Every table carries `user_id`; queue, budget, mutes, memory, jobs and timezone are per user. Composio events are
routed by their entity user id (our uuid), Telegram messages by chat id. The control channel is an adapter
(`Content.choices`, `ControlEvent`), so a WhatsApp client can replace Telegram without touching workers.

## Layout

Architecture and the decisions behind it: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Setup checklist: [docs/SETUP-CHECKLIST.md](docs/SETUP-CHECKLIST.md). Migrations are numbered SQL files in `db/migrations/`, applied by `db/migrate.py`.

## License

MIT
