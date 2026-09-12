# Running Peyk locally

You need a Telegram bot, a Composio account (OAuth, triggers and actions for Gmail, Calendar, Notion, Drive and
Docs), and access to Claude on Amazon Bedrock. Everything else runs on your machine. Budget about an hour the
first time.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it)
- Docker (Postgres 16 with pgvector runs in `docker compose`)
- AWS CLI, only if you use Bedrock (`brew install awscli`)

```bash
git clone https://github.com/akcware/peyk && cd peyk
cp .env.example .env
uv sync --group dev
```

## 1. Telegram bot

1. In Telegram, open **@BotFather** → `/newbot` → copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Press **Start** on your new bot once (a bot may only message people who started it).
3. Get your chat id and put it in `.env` as `TELEGRAM_CHAT_ID`:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -c "import sys,json; [print(u['message']['chat']['id']) for u in json.load(sys.stdin)['result'] if 'message' in u]"
```

`TELEGRAM_CHAT_ID` is only needed for the bootstrap user, the one whose `USER_ID`/`COMPOSIO_USER_ID` from `.env`
is migrated into the database at start-up. Anyone else who messages the bot is created as a new user and
onboarded in chat.

## 2. Composio

1. Create an account at https://platform.composio.dev, then **Settings → API keys** → `.env` `COMPOSIO_API_KEY`.
2. Pick the Composio user id you will use for your own account (`COMPOSIO_USER_ID`, `default` is fine).
3. Create an auth config per toolkit you want. Composio's managed OAuth app is enough, no Google Cloud project
   needed. The setup script can do it and prints the id to put in `.env`:

```bash
uv run python scripts/composio_setup.py --toolkit gmail --create-auth-config
```

   Repeat for `googlecalendar`, `notion`, `googledrive`, `googledocs` as you like. The ids go into
   `COMPOSIO_GMAIL_AUTH_CONFIG_ID`, `COMPOSIO_CALENDAR_AUTH_CONFIG_ID`, `COMPOSIO_NOTION_AUTH_CONFIG_ID`,
   `COMPOSIO_DRIVE_AUTH_CONFIG_ID`, `COMPOSIO_DOCS_AUTH_CONFIG_ID`. They are also what the agent uses when it
   offers a connection to a new person in chat, so set them even if you connect your own account elsewhere.
4. Connect your own account. Either let Peyk do it in chat once the workers run, or run the script without
   `--create-auth-config`: it prints an OAuth link, waits, and enables the toolkit's triggers.
5. Leave `COMPOSIO_DELIVERY=ws`. Events arrive over Composio's realtime socket and no public URL is needed.
   Switch to `webhook` only if you want the FastAPI gateway to receive events; then expose it
   (`ngrok http 8000`), register the URL with `scripts/composio_setup.py --toolkit gmail --webhook https://…/webhook/composio`,
   and copy the webhook secret from the dashboard into `COMPOSIO_WEBHOOK_SECRET`.
6. `COMPOSIO_TOOLKIT_VERSIONS` pins the toolkit versions used for manual action execution (Composio refuses
   "latest"). `scripts/composio_setup.py --toolkit gmail --status` shows the current and pinned version.

Useful flags of the setup script: `--status` (connections and active triggers), `--payload-schema` (trigger
payload shape), `--disable <trigger id>` / `--enable <trigger id>` (for the outage tests).

## 3. Models on Bedrock

1. Region `us-east-1` (`AWS_REGION`). In the Bedrock console request model access for the Anthropic Claude
   family; the first request needs a short use-case form.
2. Credentials: a named profile with a long-lived IAM key is the least painful for local work
   (`aws configure --profile peyk`, then `AWS_PROFILE=peyk` in `.env`). Bedrock invoke rights are enough for the
   runtime. Leave `AWS_PROFILE` empty to use the default credential chain.
3. Model ids are cross-region inference profiles and come from the Bedrock model catalog:

```
TRIAGE_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
CHAT_MODEL_ID=us.anthropic.claude-sonnet-4-6
```

   Check what your account can see:

```bash
aws bedrock list-foundation-models --region us-east-1 --by-provider anthropic --query 'modelSummaries[].modelId'
```

4. Memory uses Titan Text Embeddings v2 (`amazon.titan-embed-text-v2:0`), enabled with the same model access.

Without Bedrock: set `MODEL_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`, and use Anthropic model ids. Triage and
chat work the same way; memory still needs Bedrock for embeddings.

## 4. Run

```bash
make migrate        # starts compose Postgres on port 5433 and applies db/migrations
make run-workers    # ingest + scheduler + consumer loop + /health on 8080
```

Message your bot. Say hi; Peyk introduces itself, asks who you are and offers to connect a service. Send yourself a
mail and watch the log lines `triage.decided … notify=true|false`.

Only one workers process may run per bot and Composio project: Telegram `getUpdates` and the Composio socket are
single-consumer. Stop the local process before deploying, and do not run it while production is up.

Other targets:

| Command | What it does |
|---|---|
| `make test` | the whole suite except model evals (`-m "not llm"`), against `TEST_DATABASE_URL` |
| `make gate-1-llm` | the triage eval against the real model (60 labelled mails) |
| `make status` | observations per source and status |
| `make requeue-failed` | put `failed` observations back on the queue after fixing the cause |
| `make reset-user CHAT=<chat id>` | forget a person completely, to test onboarding again |
| `make lint` | ruff |
| `docker compose --profile app up` | run workers (and gateway) in containers instead |

## Environment reference

| Variable | Purpose |
|---|---|
| `USER_ID`, `USER_LANGUAGE`, `USER_PROFILE` | bootstrap user; leave the profile empty, Peyk asks and learns with consent |
| `DATABASE_URL`, `TEST_DATABASE_URL`, `NEON_DATABASE_URL` | local DB, test DB, production DB (deploy script maps it to `DATABASE_URL`) |
| `ADAPTERS`, `CONTROL_SOURCE` | adapters loaded by the registry (`composio,telegram`); the person's own channel |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | bot token; chat id of the bootstrap user |
| `COMPOSIO_API_KEY`, `COMPOSIO_USER_ID`, `COMPOSIO_*_AUTH_CONFIG_ID` | Composio access; one auth config per toolkit |
| `COMPOSIO_DELIVERY`, `COMPOSIO_WEBHOOK_SECRET` | `ws` (default) or `webhook` + its secret |
| `COMPOSIO_TOOLKIT_VERSIONS` | pinned toolkit versions for action execution |
| `MODEL_PROVIDER`, `AWS_REGION`, `AWS_PROFILE`, `TRIAGE_MODEL_ID`, `CHAT_MODEL_ID`, `ANTHROPIC_API_KEY` | models |
| `AGENT_MODE`, `AGENTCORE_RUNTIME_ARN` | `local` (in-process agent) or `agentcore` + the deployed runtime ARN |
| `TIMEZONE`, `MORNING_BRIEF_AT`, `RECONCILE_EVERY`, `RECONCILE_LOOKBACK_MINUTES` | scheduler defaults; empty string disables a default job |
| `HEALTH_PORT`, `LOG_LEVEL` | health endpoint (0 disables), structlog level |

## Troubleshooting

- **Nothing arrives from Gmail.** `scripts/composio_setup.py --toolkit gmail --status` must list an active account
  and an enabled `GMAIL_NEW_GMAIL_MESSAGE` trigger. Composio polls about once a minute, so expect 10–60 s.
- **Events were lost while the workers were down.** Expected: Composio does not replay. The `reconcile` job backfills
  the gap every 10 minutes.
- **boto3 complains about the profile.** An empty `AWS_PROFILE=` is fine (the workers drop it); a profile name that
  does not exist is not.
- **"model not available for this account".** Ask for model access in the Bedrock console, or check the id against
  `list-foundation-models`.
- **Two bots answering, or none.** Two workers processes share one bot. Stop one.
