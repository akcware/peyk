# Deploying Peyk

Production is one long-lived container on Fly.io, a Neon Postgres database, and Claude on Amazon Bedrock. The
container runs everything: Composio realtime subscription, Telegram long-polling, scheduler, the consumer loop
and an HTTP `/health` endpoint on 8080. No gateway is needed while `COMPOSIO_DELIVERY=ws`.

Exactly **one** instance must run: Telegram `getUpdates` and the Composio subscription are single-consumer.
`fly.toml` pins one machine with auto-stop off, and you must stop any local `make run-workers` before deploying.

> **Why not AWS compute?** The hackathon AWS account sits in an Organization whose service control policy
> denies CloudFormation, ECR, CodeBuild, AgentCore, Lightsail, EC2, ECS and App Runner. Bedrock is open, so the
> models stay on Bedrock and the worker runs on Fly.io. `scripts/deploy_lightsail.sh` and the `agentcore/`
> configuration are kept for an account without that policy (see below).

## One-time setup

1. **Neon.** Create a project (Postgres 16, an AWS us-east region; ours is us-east-2, next to Fly's `iad`). Put the
   *direct* connection string in `.env` as `NEON_DATABASE_URL=postgresql://…?sslmode=require`. Migrations,
   including `create extension vector`, run on container start.
2. **IAM key for Bedrock.** Create a user (ours is `peyk-runtime`) with Bedrock invoke rights (voice notes go through
   Bedrock too, Voxtral via Converse; add `transcribe:StartStreamTranscription` only for `STT_ENGINE=transcribe`) and store its
   key in the local AWS profile `peyk` (`aws configure --profile peyk`). The deploy script copies that key into
   the container environment; it never touches your admin credentials.
3. **Fly.io.** `brew install flyctl && flyctl auth login`. The account needs a payment method; the
   shared-cpu-1x / 1 GB machine costs about $5 per month.

## Deploy

```bash
scripts/deploy_fly.sh --create   # first time: create the app, stage secrets from .env + the peyk key, deploy
make deploy                      # later deploys (secrets re-staged, one restart)
make deploy-logs                 # live logs
```

Fly builds the Dockerfile remotely, runs one machine in `iad`, and probes `/health` every 30 s. Health is 200 only
when the database answers and every loop has beaten in the last 5 minutes; Fly restarts the machine otherwise.
The image runs as a non-root user and applies pending migrations in its entrypoint before starting the workers.

## Runtime notes

- Secrets live in Fly secrets (encrypted at rest, injected as env). Rotate the Bedrock key by creating a new one,
  redeploying, then deleting the old one.
- Logs are JSON (`structlog`), `LOG_LEVEL=INFO`. Worker lines carry `observation_id` and `user_id`.
- Composio's realtime socket is watched every 15 s; a dead socket ends the ingest iterator, which restarts with
  exponential backoff (1 s → 60 s). Events missed meanwhile are picked up by `reconcile` every 10 minutes.
- Cost: Fly ≈ $5 per month, Neon free tier, Bedrock usage (Haiku triage ≈ $0.001 per mail).

## Local vs. production

| | local | production |
|---|---|---|
| DB | compose Postgres on 5433 | Neon (`NEON_DATABASE_URL` → `DATABASE_URL`) |
| AWS credentials | `AWS_PROFILE=peyk` | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` of the same key |
| Composio events | websocket | websocket |
| Agent | in-process (`AGENT_MODE=local`) | in-process |
| Health | `HEALTH_PORT=8080`, optional | required by Fly's check |

## Bedrock AgentCore (on an unrestricted account)

The `agent/` package is written to run on AgentCore Runtime: no database, no imports from the rest of the repo,
an `app.py` entrypoint that routes `{"task": …}` payloads. `agentcore/agentcore.json` declares it as a CodeZip
runtime (Python 3.12, HTTP protocol, IAM auth) with the model ids as environment variables.

```bash
uv sync --group agentcore          # bedrock-agentcore SDK + starter toolkit
cd agentcore && agentcore validate && agentcore deploy
```

Then set `AGENT_MODE=agentcore` and `AGENTCORE_RUNTIME_ARN=<the deployed runtime ARN>` for the workers; they
switch from the in-process call to `invoke_agent_runtime` with the same payloads. The generic CLI reference is in
[agentcore/CLI-REFERENCE.md](../agentcore/CLI-REFERENCE.md). Packaging and `agentcore validate` were verified
locally; the deploy itself has not run because of the policy above, so treat the first deploy as untested.
