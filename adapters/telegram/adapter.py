"""Telegram via Bot API directly with httpx. Four endpoints: getUpdates, sendMessage, editMessageText,
answerCallbackQuery. No library, so the output channel's failure modes are visible."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from core.adapter import Capabilities
from core.config import Settings, get_settings
from core.log import get_logger
from core.models import Connection, Content, Observation

log = get_logger("telegram.adapter")


class TelegramAdapter:
    id = "telegram"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http: httpx.AsyncClient | None = None,
        poll_timeout: int = 25,
    ) -> None:
        self._settings = settings or get_settings()
        self._http = http
        self._poll_timeout = poll_timeout
        self._offset: int | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=f"https://api.telegram.org/bot{self._settings.TELEGRAM_BOT_TOKEN}",
                timeout=httpx.Timeout(self._poll_timeout + 10),
            )
        return self._http

    async def _call(self, method: str, **params: Any) -> Any:
        resp = await self.http.post(f"/{method}", json=params)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {body.get('description')} (params={list(params)})")
        return body["result"]

    def capabilities(self) -> Capabilities:
        return {"can_send": True, "needs_session": False, "needs_user_device": False}

    async def connect(self, user_id: UUID) -> Connection:
        conn = Connection(adapter_id=self.id, user_id=user_id)
        if not self._settings.TELEGRAM_BOT_TOKEN:
            log.warning("telegram.no_token", hint="set TELEGRAM_BOT_TOKEN")
            return conn
        me = await self._call("getMe")
        conn.data["bot_username"] = me.get("username")
        conn.data["chat_id"] = self._settings.TELEGRAM_CHAT_ID
        log.info("telegram.connected", bot=me.get("username"))
        return conn

    @staticmethod
    def update_to_observation(update: dict[str, Any], user_id: UUID) -> Observation | None:
        """Pure: one Telegram update -> Observation. message and callback_query only."""
        update_id = update.get("update_id")
        if update_id is None:
            return None
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            occurred = datetime.fromtimestamp(msg.get("date", 0), tz=UTC) if msg.get("date") else datetime.now(tz=UTC)
        elif "callback_query" in update:
            cq = update["callback_query"]
            chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
            occurred = datetime.now(tz=UTC)
        else:
            return None
        return Observation(
            user_id=user_id,
            source="telegram",
            source_key=str(update_id),
            kind="message_in",
            occurred_at=occurred,
            thread_key=str(chat_id) if chat_id is not None else None,
            payload=update,
        )

    async def subscribe(self, conn: Connection) -> AsyncIterator[Observation]:
        if not self._settings.TELEGRAM_BOT_TOKEN:
            return
        while True:
            params: dict[str, Any] = {"timeout": self._poll_timeout, "allowed_updates": ["message", "callback_query"]}
            if self._offset is not None:
                params["offset"] = self._offset
            try:
                updates = await self._call("getUpdates", **params)
            except (httpx.HTTPError, RuntimeError) as e:
                log.warning("telegram.poll_error", error=str(e))
                await asyncio.sleep(2)
                continue
            for update in updates:
                self._offset = max(self._offset or 0, update["update_id"] + 1)
                obs = self.update_to_observation(update, conn.user_id)
                if obs is not None:
                    yield obs

    async def backfill(self, conn: Connection, since: datetime) -> AsyncIterator[Observation]:
        return
        yield  # Bot API has no history; nothing to backfill.

    async def send(self, conn: Connection, thread_key: str, content: Content) -> str:
        params: dict[str, Any] = {"chat_id": thread_key, "text": content.text}
        if content.reply_markup:
            params["reply_markup"] = content.reply_markup
        if content.extra.get("parse_mode"):
            params["parse_mode"] = content.extra["parse_mode"]
        result = await self._call("sendMessage", **params)
        return str(result["message_id"])

    async def edit_message(self, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        await self._call("editMessageText", **params)

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        await self._call("answerCallbackQuery", **params)
