"""Telegram via Bot API directly with httpx. Five endpoints: getUpdates, sendMessage, editMessageText,
answerCallbackQuery, getFile (voice notes). No library, so the output channel's failure modes are visible."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from core.adapter import Capabilities, UserDirectory
from core.config import Settings, get_settings
from core.log import get_logger
from core.models import Connection, Content, Observation
from core.stt import (
    TELEGRAM_VOICE_ENCODING,
    TELEGRAM_VOICE_SAMPLE_RATE,
    Transcriber,
    language_options,
    voice_failed_text,
    voice_text,
)

log = get_logger("telegram.adapter")


class TelegramAdapter:
    id = "telegram"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http: httpx.AsyncClient | None = None,
        poll_timeout: int = 25,
        users: UserDirectory | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http = http
        self._poll_timeout = poll_timeout
        self._offset: int | None = None
        self._users = users
        self._transcriber = transcriber

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
                if obs is None:
                    continue
                frm = (update.get("message") or update.get("callback_query") or {}).get("from") or {}
                name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or None
                lang = (frm.get("language_code") or "").split("-")[0].lower() or None   # Telegram client language
                if self._users is not None and obs.thread_key:
                    obs.user_id = await self._users.resolve_control(self.id, obs.thread_key, display_name=name, language=lang)
                msg = update.get("message") or {}
                if msg.get("voice"):
                    await self.attach_voice_text(obs, msg, display_name=name, language=lang)
                yield obs

    async def attach_voice_text(self, obs: Observation, msg: dict[str, Any], *, display_name: str | None,
                                language: str | None) -> None:
        """A voice note becomes text before anyone downstream sees it: payload['control'] carries the transcript
        (bracketed as an automatic one) and payload['voice'] what happened. A failure never drops the message;
        the agent gets a note it can relay instead."""
        voice = msg["voice"]
        duration = int(voice.get("duration") or 0)
        control: dict[str, Any] = {"message_id": msg.get("message_id"), "display_name": display_name}
        note: dict[str, Any] = {"duration_s": duration, "file_id": voice.get("file_id")}
        try:
            if self._transcriber is None:
                raise RuntimeError("no transcriber configured")
            if duration > self._settings.VOICE_MAX_S:
                raise ValueError(f"longer than {self._settings.VOICE_MAX_S} s")
            audio = await self.download_file(str(voice["file_id"]))
            langs = language_options(language, self._settings.stt_languages)
            t = await self._transcriber.transcribe(audio, media_encoding=TELEGRAM_VOICE_ENCODING,
                                                   sample_rate_hz=TELEGRAM_VOICE_SAMPLE_RATE, languages=langs)
            note.update(language=t.language, took_ms=t.took_ms, chars=len(t.text))
            control["text"] = voice_text(t.text, duration)
            log.info("telegram.voice_transcribed", chat=obs.thread_key, duration_s=duration, language=t.language,
                     ms=t.took_ms, chars=len(t.text))
        except Exception as e:  # noqa: BLE001 - the message must still reach the agent
            note["error"] = str(e)
            control["text"] = voice_failed_text(duration, str(e))
            log.warning("telegram.voice_failed", chat=obs.thread_key, duration_s=duration, error=str(e))
        obs.payload = {**obs.payload, "control": control, "voice": note}

    async def download_file(self, file_id: str) -> bytes:
        """getFile, then the file endpoint (a different URL prefix than the bot methods). Bot API serves up to 20 MB."""
        info = await self._call("getFile", file_id=file_id)
        path = info.get("file_path")
        if not path:
            raise RuntimeError("telegram getFile returned no file_path")
        url = f"https://api.telegram.org/file/bot{self._settings.TELEGRAM_BOT_TOKEN}/{path}"
        resp = await self.http.get(url)
        resp.raise_for_status()
        return resp.content

    async def backfill(self, conn: Connection, since: datetime) -> AsyncIterator[Observation]:
        return
        yield  # Bot API has no history; nothing to backfill.

    async def send(self, conn: Connection, thread_key: str, content: Content) -> str:
        params: dict[str, Any] = {"chat_id": thread_key, "text": content.text}
        if content.reply_markup:
            params["reply_markup"] = content.reply_markup
        elif content.choices:
            params["reply_markup"] = {"inline_keyboard": [[{"text": c.text, "callback_data": c.data} for c in content.choices]]}
        if content.extra.get("parse_mode"):
            params["parse_mode"] = content.extra["parse_mode"]
        result = await self._call("sendMessage", **params)
        return str(result["message_id"])

    async def edit_message(self, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        await self._call("editMessageText", **params)

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        await self._call("sendChatAction", chat_id=chat_id, action=action)

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        await self._call("answerCallbackQuery", **params)
