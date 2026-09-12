#!/usr/bin/env bash
# Deploy the workers container to Fly.io.
#   scripts/deploy_fly.sh --create   # first time: create the app, push secrets, deploy
#   scripts/deploy_fly.sh            # later: push secrets that changed (staged, no restart) + deploy
#   scripts/deploy_fly.sh --logs     # live logs
# Secrets come from .env (never committed) and the local AWS profile `peyk` (Bedrock-only IAM key).
set -euo pipefail
cd "$(dirname "$0")/.."
APP="${FLY_APP:-peyk}"
KEY_PROFILE="${PEYK_KEY_PROFILE:-peyk}"

envval() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//' | sed -E 's/^"(.*)"$/\1/'; }
need() { [ -n "$(envval "$1")" ] || { echo "missing $1 in .env" >&2; exit 1; }; }

if [ "${1:-}" = "--logs" ]; then exec flyctl logs -a "$APP"; fi

need NEON_DATABASE_URL; need TELEGRAM_BOT_TOKEN; need COMPOSIO_API_KEY; need TRIAGE_MODEL_ID; need CHAT_MODEL_ID
AK=$(aws configure get aws_access_key_id --profile "$KEY_PROFILE"); SK=$(aws configure get aws_secret_access_key --profile "$KEY_PROFILE")
[ -n "$AK" ] && [ -n "$SK" ] || { echo "profile $KEY_PROFILE has no access key" >&2; exit 1; }

if [ "${1:-}" = "--create" ]; then
  flyctl apps create "$APP" --org "${FLY_ORG:-personal}" >/dev/null 2>&1 || echo "app $APP exists"
fi

# Secrets: everything sensitive or user-specific. Staged so the machine restarts once, on deploy.
SECRET_KEYS=(USER_ID USER_PROFILE USER_LANGUAGE TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID COMPOSIO_API_KEY COMPOSIO_USER_ID
  COMPOSIO_GMAIL_AUTH_CONFIG_ID COMPOSIO_CALENDAR_AUTH_CONFIG_ID COMPOSIO_NOTION_AUTH_CONFIG_ID COMPOSIO_DRIVE_AUTH_CONFIG_ID
  COMPOSIO_DOCS_AUTH_CONFIG_ID COMPOSIO_TOOLKIT_VERSIONS MODEL_PROVIDER AWS_REGION TRIAGE_MODEL_ID CHAT_MODEL_ID
  TIMEZONE MORNING_BRIEF_AT RECONCILE_EVERY RECONCILE_LOOKBACK_MINUTES ADAPTERS CONTROL_SOURCE)
ARGS=()
for k in "${SECRET_KEYS[@]}"; do v=$(envval "$k"); [ -n "$v" ] && ARGS+=("$k=$v"); done
ARGS+=("DATABASE_URL=$(envval NEON_DATABASE_URL)" "AWS_ACCESS_KEY_ID=$AK" "AWS_SECRET_ACCESS_KEY=$SK")
flyctl secrets set --stage -a "$APP" "${ARGS[@]}" >/dev/null
echo "secrets staged (${#ARGS[@]})"

flyctl deploy -a "$APP" --ha=false --yes
flyctl status -a "$APP"
echo "health: https://$APP.fly.dev/health"
