# Proactive Agent

A personal agent that *watches* your channels (Gmail, Calendar, Telegram) and interrupts you only when it matters.
Built for the [Agents for Humans](https://agentsforhumans.devpost.com/) hackathon with Strands Agents on Amazon Bedrock.

> Status: **Phase 0** — pipeline with zero intelligence. Mail in Gmail → raw Telegram notification. No LLM yet.

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
- [ ] Disable the trigger in Composio → send a mail → re-enable. Outcome (pick one): `event arrived late, single notification` / `event never arrived`. Phase 2's `reconcile` job exists for the second case.
- [ ] With workers running: `docker compose kill workers` → send a mail → start workers → mail arrives, nothing lost.

## Layout

See `docs/` for the PRD-derived checklist. Migrations are numbered SQL files in `db/migrations/`, applied by `db/migrate.py`.

## License

MIT
