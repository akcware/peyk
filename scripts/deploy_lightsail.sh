#!/usr/bin/env bash
# Deploy the workers container to AWS Lightsail Container Service (no ECR/CloudFormation needed).
#
#   scripts/deploy_lightsail.sh            # build, push, deploy
#   scripts/deploy_lightsail.sh --create   # first time: also create the service (power micro, scale 1)
#   scripts/deploy_lightsail.sh --logs     # tail recent container logs
#
# Reads secrets from .env (never committed). The container gets: DATABASE_URL=NEON_DATABASE_URL,
# AWS_ACCESS_KEY_ID/SECRET from the `peyk` profile (long-lived IAM key with Bedrock-only rights),
# and the Telegram/Composio/model settings. HEALTH_PORT 8080 is the service's public endpoint.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE="${LIGHTSAIL_SERVICE:-peyk}"
REGION="${LIGHTSAIL_REGION:-us-east-2}"        # Lightsail region (near the Neon DB); Bedrock region stays AWS_REGION from .env
POWER="${LIGHTSAIL_POWER:-micro}"
AWS_PROFILE_ADMIN="${LIGHTSAIL_ADMIN_PROFILE:-default}"   # the profile that may manage Lightsail
KEY_PROFILE="${PEYK_KEY_PROFILE:-peyk}"                  # the profile whose key the container will use

envval() { { grep -E "^$1=" .env || true; } | head -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//' | sed -E 's/^"(.*)"$/\1/'; }
need() { [ -n "$(envval "$1")" ] || { echo "missing $1 in .env" >&2; exit 1; }; }

if [ "${1:-}" = "--logs" ]; then
  aws lightsail get-container-log --profile "$AWS_PROFILE_ADMIN" --region "$REGION" --service-name "$SERVICE" --container-name workers \
    --query 'logEvents[-80:].[createdAt,message]' --output text
  exit 0
fi

need NEON_DATABASE_URL; need TELEGRAM_BOT_TOKEN; need COMPOSIO_API_KEY; need TRIAGE_MODEL_ID; need CHAT_MODEL_ID
AK=$(aws configure get aws_access_key_id --profile "$KEY_PROFILE"); SK=$(aws configure get aws_secret_access_key --profile "$KEY_PROFILE")
[ -n "$AK" ] && [ -n "$SK" ] || { echo "profile $KEY_PROFILE has no access key" >&2; exit 1; }

if [ "${1:-}" = "--create" ]; then
  aws lightsail create-container-service --profile "$AWS_PROFILE_ADMIN" --region "$REGION" \
    --service-name "$SERVICE" --power "$POWER" --scale 1 --tags key=project,value=peyk >/dev/null
  echo "creating service $SERVICE ($POWER) — waiting until READY"
  until [ "$(aws lightsail get-container-services --profile "$AWS_PROFILE_ADMIN" --region "$REGION" --service-name "$SERVICE" --query 'containerServices[0].state' --output text)" = "READY" ]; do sleep 15; printf .; done; echo
fi

echo "building linux/amd64 image"
docker build --platform linux/amd64 -t peyk-workers:latest . >/dev/null
echo "pushing to Lightsail"
PUSH_OUT=$(aws lightsail push-container-image --profile "$AWS_PROFILE_ADMIN" --region "$REGION" --service-name "$SERVICE" --label workers --image peyk-workers:latest)
IMAGE=$(echo "$PUSH_OUT" | grep -oE ':'"$SERVICE"'\.workers\.[0-9]+' | tail -1)
[ -n "$IMAGE" ] || { echo "could not parse pushed image name:"; echo "$PUSH_OUT"; exit 1; }
echo "pushed $IMAGE"

ENV_JSON=$(python3 - "$AK" "$SK" <<'PY'
import json, re, sys, pathlib
ak, sk = sys.argv[1], sys.argv[2]
env = {}
for line in pathlib.Path(".env").read_text().splitlines():
    if not line or line.startswith("#") or "=" not in line: continue
    k, v = line.split("=", 1); v = re.sub(r"\s+#.*$", "", v).strip().strip('"')
    env[k.strip()] = v
keep = ["USER_ID","USER_PROFILE","USER_LANGUAGE","ADAPTERS","CONTROL_SOURCE","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID",
        "COMPOSIO_API_KEY","COMPOSIO_USER_ID","COMPOSIO_GMAIL_AUTH_CONFIG_ID","COMPOSIO_CALENDAR_AUTH_CONFIG_ID",
        "COMPOSIO_NOTION_AUTH_CONFIG_ID","COMPOSIO_DRIVE_AUTH_CONFIG_ID","COMPOSIO_DOCS_AUTH_CONFIG_ID","COMPOSIO_TOOLKIT_VERSIONS",
        "MODEL_PROVIDER","AWS_REGION","TRIAGE_MODEL_ID","CHAT_MODEL_ID","TIMEZONE","MORNING_BRIEF_AT","RECONCILE_EVERY","RECONCILE_LOOKBACK_MINUTES"]
out = {k: env[k] for k in keep if env.get(k)}
out.update({"DATABASE_URL": env["NEON_DATABASE_URL"], "COMPOSIO_DELIVERY": "ws", "AGENT_MODE": "local", "AWS_PROFILE": "",
            "AWS_ACCESS_KEY_ID": ak, "AWS_SECRET_ACCESS_KEY": sk, "HEALTH_PORT": "8080", "LOG_LEVEL": env.get("LOG_LEVEL", "INFO")})
print(json.dumps(out))
PY
)
DEPLOY_JSON=$(python3 -c "import json,sys; env=json.loads(sys.argv[1]); print(json.dumps({'containers': {'workers': {'image': sys.argv[2], 'command': ['workers'], 'environment': env, 'ports': {'8080': 'HTTP'}}}, 'publicEndpoint': {'containerName': 'workers', 'containerPort': 8080, 'healthCheck': {'path': '/health', 'intervalSeconds': 30, 'timeoutSeconds': 5, 'healthyThreshold': 2, 'unhealthyThreshold': 3, 'successCodes': '200'}}}))" "$ENV_JSON" "$IMAGE")
aws lightsail create-container-service-deployment --profile "$AWS_PROFILE_ADMIN" --region "$REGION" --service-name "$SERVICE" --cli-input-json "$DEPLOY_JSON" >/dev/null
echo "deployment submitted; waiting for RUNNING"
until [ "$(aws lightsail get-container-services --profile "$AWS_PROFILE_ADMIN" --region "$REGION" --service-name "$SERVICE" --query 'containerServices[0].currentDeployment.state' --output text)" = "ACTIVE" ]; do sleep 15; printf .; done; echo
URL=$(aws lightsail get-container-services --profile "$AWS_PROFILE_ADMIN" --region "$REGION" --service-name "$SERVICE" --query 'containerServices[0].url' --output text)
echo "live: ${URL}health"
