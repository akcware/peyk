"""Forget a user completely (for re-testing onboarding): all rows keyed by that user, then the app_user row.

  uv run python scripts/reset_user.py --chat 7360078725        # by Telegram chat id
  uv run python scripts/reset_user.py --chat 7360078725 --keep-user   # wipe history but keep profile/connections
"""
from __future__ import annotations

import argparse
import asyncio

import psycopg
from psycopg.rows import dict_row

from core.config import get_settings

TABLES_BY_USER = ("sent_notification", "mute_rule", "budget_settings", "memory", "scheduled_job", "chat_state", "action",
                  "identity", "person", "observation")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required=True, help="control thread key (Telegram chat id)")
    ap.add_argument("--source", default="telegram")
    ap.add_argument("--keep-user", action="store_true", help="keep the app_user row (profile, connections)")
    args = ap.parse_args()
    s = get_settings()
    async with await psycopg.AsyncConnection.connect(s.DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        cur = await conn.execute("select id, display_name from app_user where control_source = %s and control_thread_key = %s",
                                 (args.source, args.chat))
        user = await cur.fetchone()
        if user is None:
            print("no such user")
            return 1
        uid = user["id"]
        await conn.execute("delete from triage where observation_id in (select id from observation where user_id = %s)", (uid,))
        for t in TABLES_BY_USER:
            cur = await conn.execute(f"delete from {t} where user_id = %s", (uid,))
            print(f"{t:18s} -{cur.rowcount}")
        if args.keep_user:
            await conn.execute("update app_user set state = '{}'::jsonb where id = %s", (uid,))
            print("app_user kept (state cleared)")
        else:
            await conn.execute("delete from app_user where id = %s", (uid,))
            print(f"app_user deleted ({user['display_name']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
