"""One-shot Composio setup for a toolkit: connection link -> wait -> enable trigger -> (optional) webhook.

Usage:
  uv run python scripts/composio_setup.py --toolkit gmail            # link + enable GMAIL_NEW_GMAIL_MESSAGE
  uv run python scripts/composio_setup.py --toolkit gmail --status    # just show connections + active triggers
  uv run python scripts/composio_setup.py --toolkit gmail --webhook https://xxxx.ngrok.app/webhook/composio
  uv run python scripts/composio_setup.py --toolkit gmail --payload-schema   # print trigger payload schema

This is the only file outside adapters/composio/ that imports the composio SDK.
"""
from __future__ import annotations

import argparse
import json
import sys

from composio import Composio

from core.config import get_settings

TOOLKITS: dict[str, dict] = {
    "gmail": {
        "auth_config_env": "COMPOSIO_GMAIL_AUTH_CONFIG_ID",
        "triggers": {"GMAIL_NEW_GMAIL_MESSAGE": {"labelIds": "INBOX", "interval": 1, "userId": "me"}},
    },
    "googlecalendar": {  # phase 5; slug verified with --payload-schema on 2026-09-10
        "auth_config_env": "COMPOSIO_CALENDAR_AUTH_CONFIG_ID",
        "triggers": {"GOOGLECALENDAR_EVENT_STARTING_SOON_TRIGGER": {
            "calendarId": "primary", "minutesBeforeStart": 15, "countdownWindowMinutes": 5, "interval": 1, "includeAllDay": False}},
    },
}


def _env(name: str) -> str:
    """os.environ first, then a minimal .env read (core.config only models the keys it knows)."""
    import os
    from pathlib import Path

    if os.environ.get(name):
        return os.environ[name]
    env = Path(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].split("#", 1)[0].strip().strip('"')
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--toolkit", required=True, choices=sorted(TOOLKITS))
    ap.add_argument("--auth-config-id", help="overrides the env value")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--webhook", help="set project webhook subscription URL (V3)")
    ap.add_argument("--payload-schema", action="store_true", help="print trigger type payload schemas and exit")
    ap.add_argument("--no-wait", action="store_true", help="print the OAuth link and exit without waiting")
    ap.add_argument("--create-auth-config", action="store_true", help="create a Composio-managed auth config for the toolkit")
    ap.add_argument("--disable", metavar="TRIGGER_ID", help="disable a trigger instance (manual gate tests)")
    ap.add_argument("--enable", metavar="TRIGGER_ID", help="enable a trigger instance")
    args = ap.parse_args()

    s = get_settings()
    if not s.COMPOSIO_API_KEY:
        sys.exit("COMPOSIO_API_KEY missing in .env")
    c = Composio(api_key=s.COMPOSIO_API_KEY, toolkit_versions=s.composio_toolkit_versions or None)
    tk = TOOLKITS[args.toolkit]
    user_id = s.COMPOSIO_USER_ID

    if args.disable or args.enable:
        tid = args.disable or args.enable
        r = c.client.trigger_instances.manage.update(tid, status="disable" if args.disable else "enable")
        print(f"trigger {tid}: {'disabled' if args.disable else 'enabled'} -> {r}")
        return 0

    if args.payload_schema:
        for slug in tk["triggers"]:
            t = c.triggers.get_type(slug)
            print(f"== {slug}")
            print(json.dumps(getattr(t, "payload", None) or t.model_dump(), indent=2, default=str)[:6000])
        return 0

    accounts = c.client.connected_accounts.list(user_ids=[user_id], toolkit_slugs=[args.toolkit])
    active = [a for a in (accounts.items or []) if getattr(a, "status", "") == "ACTIVE"]
    print(f"user_id={user_id} toolkit={args.toolkit} active_accounts={[a.id for a in active]}")

    if args.status:
        meta = c.client.toolkits.retrieve(args.toolkit).model_dump().get("meta") or {}
        pinned = s.composio_toolkit_versions.get(args.toolkit)
        print(f"toolkit version: current={meta.get('version')} pinned={pinned or '(none — set COMPOSIO_TOOLKIT_VERSIONS)'}")
        act = c.triggers.list_active(connected_account_ids=[a.id for a in active] or None)
        for t in getattr(act, "items", []) or []:
            print(f"  trigger {t.trigger_name} id={t.id} disabled={getattr(t, 'disabled_at', None)}")
        return 0

    if not active:
        auth_config_id = args.auth_config_id or _env(tk["auth_config_env"])
        if not auth_config_id and args.create_auth_config:
            created = c.auth_configs.create(args.toolkit, {"type": "use_composio_managed_auth"})
            auth_config_id = created.id
            print(f"created auth config {auth_config_id}; put in .env: {tk['auth_config_env']}={auth_config_id}")
        if not auth_config_id:
            sys.exit(f"no active {args.toolkit} account and {tk['auth_config_env']} not set (use --create-auth-config)")
        req = c.connected_accounts.link(user_id, auth_config_id)
        print("\nOpen this link and finish OAuth:\n  ", req.redirect_url, "\n")
        if args.no_wait:
            return 0
        acc = req.wait_for_connection(timeout=300)
        print(f"connected: id={acc.id} status={acc.status}")
        account_id = acc.id
    else:
        account_id = active[0].id

    for slug, cfg in tk["triggers"].items():
        r = c.triggers.create(slug, user_id=user_id, connected_account_id=account_id, trigger_config=cfg)
        print(f"trigger {slug}: id={getattr(r, 'trigger_id', r)}")

    if args.webhook:
        sub = c.triggers.set_webhook_subscription(webhook_url=args.webhook)
        print("webhook subscription:", sub)
        print("copy the webhook secret from the dashboard into COMPOSIO_WEBHOOK_SECRET")

    print(f"\nput in .env: COMPOSIO_GMAIL_CONNECTED_ACCOUNT_ID={account_id}" if args.toolkit == "gmail" else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
