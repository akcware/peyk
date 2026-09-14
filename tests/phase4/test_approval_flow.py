"""ApprovalFlow regressions found in production on 2026-09-13.

- The connection handle cache was keyed by adapter only: after a restart, everyone's mail went out through the
  Composio entity of whoever pressed Send first (user A's reply was attempted from user B's Gmail -> 404).
- A failed send showed Composio's raw developer text to the person.
- A repeated Send on the same failure made Telegram reject the edit ("message is not modified") and the fallback
  posted the same text again as a new message, once per press.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from core.adapter import AdapterRegistry
from core.models import Connection, Observation
from core.phrases import phrase
from core.repo import observation_repo, user_repo
from workers import actions
from workers.approval import ApprovalFlow, send_failure_key

COMPOSIO_404 = ("composio send failed: Cannot access thread (thread_id: '1a09be64f4ba77bf'). Gmail returned a 404 error, "
                "which occurs in two scenarios:\n1. ACCESS ISSUE ...\nTo resolve: Use GMAIL_LIST_THREADS or GMAIL_FETCH_EMAILS "
                "to retrieve thread IDs from YOUR inbox.")
TG_NOT_MODIFIED = ("telegram editMessageText failed: Bad Request: message is not modified: specified new message content "
                   "and reply markup are exactly the same as a current content and reply markup of the message")


class FakeMailAdapter:
    id = "composio"

    def __init__(self, fail_with: str | None = None) -> None:
        self.connects: list[UUID] = []
        self.sends: list[tuple[str, str, str]] = []     # (composio entity used, thread_key, body)
        self.fail_with = fail_with

    def capabilities(self):
        return {"can_send": True, "needs_session": False, "needs_user_device": False}

    async def connect(self, user_id: UUID) -> Connection:
        self.connects.append(user_id)
        return Connection(adapter_id=self.id, user_id=user_id, data={"composio_user_id": f"entity-{user_id}"})

    async def send(self, conn: Connection, thread_key: str, content) -> str:
        self.sends.append((conn.data["composio_user_id"], thread_key, content.text))
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return "gmail-msg-1"


class FakeNotifier:
    language = "tr"

    def __init__(self, edit_error: str | None = None) -> None:
        self.markups: list[tuple[str, dict]] = []
        self.texts: list[str] = []
        self.edits: list[tuple[int, str, dict | None]] = []
        self.edit_error = edit_error
        self._mid = 100

    async def send_markup(self, text: str, markup: dict) -> int:
        self._mid += 1
        self.markups.append((text, markup))
        return self._mid

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def edit_message(self, message_id: int, text: str, markup: dict | None) -> None:
        if self.edit_error:
            raise RuntimeError(self.edit_error)
        self.edits.append((message_id, text, markup))


def _draft_obs(user_id: UUID, chat: str) -> Observation:
    return Observation(user_id=user_id, source="telegram", source_key=f"d{chat}", kind="message_in",
                       occurred_at=datetime.now(tz=UTC), thread_key=chat,
                       payload={"update_id": 1, "message": {"text": "reply to ekin", "chat": {"id": int(chat)}}})


def _approve_obs(user_id: UUID, chat: str, action: dict, tg_mid: int) -> Observation:
    data = actions.buttons(action, "tr")["inline_keyboard"][0][0]["callback_data"]
    assert data.startswith("act:approve:")
    return Observation(user_id=user_id, source="telegram", source_key=f"a{chat}{tg_mid}", kind="message_in",
                       occurred_at=datetime.now(tz=UTC), thread_key=chat,
                       payload={"update_id": 2, "callback_query": {"id": f"cq{chat}", "data": data,
                                                                    "message": {"message_id": tg_mid, "chat": {"id": int(chat)}}}})


async def _draft_and_approve(conn, flow: ApprovalFlow, user_id: UUID, chat: str, notifier: FakeNotifier, thread_key: str):
    flow.notifier = notifier
    action = await flow.on_draft(conn, _draft_obs(user_id, chat),
                                 {"body": f"hello from {chat}", "to": ["ekin@example.test"], "channel": "gmail", "thread_key": thread_key})
    tg_mid = notifier._mid
    return await flow.on_callback(conn, _approve_obs(user_id, chat, action, tg_mid))


@pytest.mark.asyncio
async def test_each_user_sends_through_their_own_connection(conn):
    adapter = FakeMailAdapter()
    flow = ApprovalFlow(AdapterRegistry({"composio": adapter}), None)
    alice, bob = uuid4(), uuid4()

    assert await _draft_and_approve(conn, flow, alice, "111", FakeNotifier(), "thread-a") == phrase("tr", "ack_sent")
    assert await _draft_and_approve(conn, flow, bob, "222", FakeNotifier(), "thread-b") == phrase("tr", "ack_sent")

    assert adapter.connects == [alice, bob]
    assert [s[0] for s in adapter.sends] == [f"entity-{alice}", f"entity-{bob}"]
    assert [s[1] for s in adapter.sends] == ["thread-a", "thread-b"]


@pytest.mark.asyncio
async def test_send_failure_shows_a_human_sentence_not_the_raw_error(conn):
    adapter = FakeMailAdapter(fail_with=COMPOSIO_404)
    flow = ApprovalFlow(AdapterRegistry({"composio": adapter}), None)
    notifier = FakeNotifier()

    assert await _draft_and_approve(conn, flow, uuid4(), "333", notifier, "thread-x") == phrase("tr", "ack_send_failed")

    _, text, markup = notifier.edits[-1]
    assert text == "⚠️ " + phrase("tr", "send_failed_thread")
    assert "GMAIL_LIST_THREADS" not in text and "404" not in text
    assert markup is not None and markup["inline_keyboard"][0][0]["callback_data"].startswith("act:approve:")   # retry stays possible
    assert notifier.texts == []


@pytest.mark.asyncio
async def test_repeated_send_on_same_failure_does_not_post_duplicates(conn):
    adapter = FakeMailAdapter(fail_with=COMPOSIO_404)
    flow = ApprovalFlow(AdapterRegistry({"composio": adapter}), None)
    notifier = FakeNotifier(edit_error=TG_NOT_MODIFIED)   # Telegram: the card already says exactly this

    await _draft_and_approve(conn, flow, uuid4(), "444", notifier, "thread-y")

    assert notifier.texts == []      # before: the whole error posted again as a new message on every press

    other = FakeNotifier(edit_error="telegram editMessageText failed: Bad Request: message to edit not found")
    await _draft_and_approve(conn, flow, uuid4(), "555", other, "thread-z")
    assert other.texts == ["⚠️ " + phrase("tr", "send_failed_thread")]   # a real edit failure still falls back to a new message


@pytest.mark.asyncio
async def test_a_thread_reply_without_an_address_goes_to_whoever_wrote_last(conn):
    """Production 2026-09-14: triage's reply card (and a chat reply in a thread) carried no address, and Gmail's reply
    tool refused every Send: "At least one of 'recipient_email', 'cc', or 'bcc' must be provided"."""
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "888")
    uid = user["id"]
    await user_repo.merge_state(conn, uid, {"emails": ["me@example.test"]})

    def mail(key: str, kind: str, sender: str, to: str, minutes_ago: int) -> Observation:
        return Observation(user_id=uid, source="gmail", source_key=key, kind=kind, thread_key="th-carla",
                           occurred_at=datetime.now(tz=UTC) - timedelta(minutes=minutes_ago),
                           payload={"from": sender, "to": to, "subject": "Apartment viewing"})

    notifier = FakeNotifier()
    flow = ApprovalFlow(AdapterRegistry({"composio": FakeMailAdapter()}), notifier)
    draft = {"body": "18:00 uyar mı?", "to": [], "channel": "gmail", "thread_key": "th-carla"}

    await observation_repo.insert(conn, mail("m1", "message_in", "Carla <Carla@Example.test>", "me@example.test", 30))
    action = await flow.on_draft(conn, _draft_obs(uid, "888"), draft)
    assert action["content"]["to"] == ["carla@example.test"]
    assert notifier.markups[-1][0].splitlines()[1] == "Kime: carla@example.test"   # the person sees who gets it

    # the person wrote last: the reply goes to whom they wrote, never back to themselves
    await observation_repo.insert(conn, mail("m2", "message_out", "Me <me@example.test>", "Carla <carla@example.test>, bo@example.test", 10))
    action = await flow.on_draft(conn, _draft_obs(uid, "888"), draft)
    assert action["content"]["to"] == ["carla@example.test", "bo@example.test"]

    # a thread we know nothing about: no guessed address
    action = await flow.on_draft(conn, _draft_obs(uid, "888"), {**draft, "thread_key": "th-unknown"})
    assert action["content"]["to"] == []


def test_send_failure_key_classifies_known_errors():
    assert send_failure_key(RuntimeError(COMPOSIO_404)) == "send_failed_thread"
    assert send_failure_key(RuntimeError("Connected account ca_x for toolkit 'gmail' is in EXPIRED state")) == "send_failed_auth"
    assert send_failure_key(RuntimeError("boom")) == "send_failed"
    for lang in ("en", "tr", "de"):
        for key in ("send_failed", "send_failed_thread", "send_failed_auth"):
            assert "{" not in phrase(lang, key)
