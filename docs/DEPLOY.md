# Deploying Peyk (Lightsail container + Neon Postgres)

One long-lived container (`workers`) does everything in production: Composio realtime subscription,
Telegram long-polling, scheduler, triage/chat/approval loop, and an HTTP `/health` endpoint on 8080.
No gateway is needed while `COMPOSIO_DELIVERY=ws`. Exactly **one** instance must run (Telegram `getUpdates`
and the Composio subscription are single-consumer) — Lightsail `scale 1`, and stop any local `make run-workers`.

## One-time setup
1. **Neon**: create a project (AWS us-east-1, Postgres 16). Put the *direct* connection string in `.env` as
   `NEON_DATABASE_URL=postgresql://…?sslmode=require`. Migrations (incl. `create extension vector`) run on
   container start.
2. **IAM key for Bedrock**: user `peyk-runtime` with Bedrock invoke-only rights; its key lives in the local
   AWS profile `peyk` (`aws configure --profile peyk`). The deploy script copies that key into the container
   environment — it never touches your admin credentials.
3. **Lightsail admin credentials**: the `default` profile (`aws login`) is only needed to run the deploy.

## Deploy
```bash
scripts/deploy_lightsail.sh --create     # first time (creates the service, ~2 min)
scripts/deploy_lightsail.sh              # every later deploy: build → push → rolling deployment
scripts/deploy_lightsail.sh --logs       # recent container logs
```
The script builds `linux/amd64`, pushes to Lightsail's own registry (no ECR), and creates a deployment with
the public endpoint bound to `/health`. Health is 200 only when the DB answers and every loop has beaten in
the last 5 minutes; Lightsail restarts the container otherwise.

## Runtime notes
- Secrets live in the Lightsail deployment environment (plain text in the AWS console, restricted to the
  account). Rotate the `peyk-runtime` key by creating a new one, redeploying, then deleting the old one.
- Logs are JSON (`structlog`); `LOG_LEVEL=INFO`. `observation_id`/`user_id` fields are on every worker line.
- Composio's realtime socket is watched every 15 s; a dead socket ends the ingest iterator, which restarts
  with exponential backoff (1 s → 60 s). Missed events are picked up by `reconcile` every 10 min.
- Cost: Lightsail micro (1 GB) $10/month + Neon free tier + Bedrock usage (Haiku triage ≈ $0.001/mail).

## Local vs. production
| | local | production |
|---|---|---|
| DB | compose Postgres on 5433 | Neon (`NEON_DATABASE_URL` → `DATABASE_URL`) |
| AWS creds | `AWS_PROFILE=peyk` | `AWS_ACCESS_KEY_ID`/`SECRET` of the same key |
| Composio events | websocket | websocket |
| Health | `HEALTH_PORT=8080` (optional) | required by Lightsail |
