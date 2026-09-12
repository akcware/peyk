# Deploying Peyk (Fly.io machine + Neon Postgres)

One long-lived container (`workers`) does everything in production: Composio realtime subscription,
Telegram long-polling, scheduler, triage/chat/approval loop, and an HTTP `/health` endpoint on 8080.
No gateway is needed while `COMPOSIO_DELIVERY=ws`. Exactly **one** instance must run (Telegram `getUpdates`
and the Composio subscription are single-consumer) — `fly.toml` pins one machine, no autostop, and you must stop
any local `make run-workers` before deploying.

> Why not AWS compute? The hackathon AWS account sits in an Organization whose SCP explicitly denies
> CloudFormation, ECR, CodeBuild, AgentCore, Lightsail, EC2, ECS and App Runner. Bedrock is open, so the models
> stay on Bedrock; the worker runs on Fly.io. `scripts/deploy_lightsail.sh` and `agentcore/` are kept for an
> account without that policy.

## One-time setup
1. **Neon**: create a project (AWS us-east-1, Postgres 16). Put the *direct* connection string in `.env` as
   `NEON_DATABASE_URL=postgresql://…?sslmode=require`. Migrations (incl. `create extension vector`) run on
   container start.
2. **IAM key for Bedrock**: user `peyk-runtime` with Bedrock invoke-only rights; its key lives in the local
   AWS profile `peyk` (`aws configure --profile peyk`). The deploy script copies that key into the container
   environment — it never touches your admin credentials.
3. **Fly.io**: `brew install flyctl && flyctl auth login` (account with a payment method; ~$5/month for the
   shared-cpu-1x/1 GB machine).

## Deploy
```bash
scripts/deploy_fly.sh --create   # first time: create app, stage secrets from .env + the peyk key, deploy
scripts/deploy_fly.sh            # later deploys (secrets re-staged, one restart)
scripts/deploy_fly.sh --logs     # live logs
```
Fly builds the Dockerfile remotely, runs one machine in `iad`, and probes `/health` every 30 s. Health is 200
only when the DB answers and every loop has beaten in the last 5 minutes; Fly restarts the machine otherwise.

## Runtime notes
- Secrets live in Fly secrets (encrypted at rest, injected as env). Rotate the `peyk-runtime` key by creating
  a new one, redeploying, then deleting the old one.
- Logs are JSON (`structlog`); `LOG_LEVEL=INFO`. `observation_id`/`user_id` fields are on every worker line.
- Composio's realtime socket is watched every 15 s; a dead socket ends the ingest iterator, which restarts
  with exponential backoff (1 s → 60 s). Missed events are picked up by `reconcile` every 10 min.
- Cost: Fly shared-cpu-1x 1 GB ≈ $5/month + Neon free tier + Bedrock usage (Haiku triage ≈ $0.001/mail).

## Local vs. production
| | local | production |
|---|---|---|
| DB | compose Postgres on 5433 | Neon (`NEON_DATABASE_URL` → `DATABASE_URL`) |
| AWS creds | `AWS_PROFILE=peyk` | `AWS_ACCESS_KEY_ID`/`SECRET` of the same key |
| Composio events | websocket | websocket |
| Health | `HEALTH_PORT=8080` (optional) | required by Fly's check |
