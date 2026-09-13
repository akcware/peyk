"""Triage worker end-to-end with a fake agent and a fake control-channel adapter. No LLM, real DB."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from agent.client import AgentClient
from agent.schemas import TriageResult
from core.models import Connection, Observation
from core.repo import budget_repo, observation_repo
from tests.conftest import USER_ID
from workers import triage


class FakeTelegram:
    id = "telegram"

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.acks: list[tuple[str, str | None]] = []
        self._n = 100

    def capabilities(self):
        return {"can_send": True, "needs_session": False, "needs_user_device": False}

    async def connect(self, user_id):
        return Connection(adapter_id=self.id, user_id=user_id)

    async def send(self, conn, thread_key, content):
        self._n += 1
        markup = content.reply_markup or ({"inline_keyboard": [[{"text": c.text, "callback_data": c.data} for c in content.choices]]}
                                          if content.choices else None)   # same rendering as the real Telegram adapter
        self.sent.append({"chat": thread_key, "text": content.text, "markup": markup, "message_id": self._n})
        return str(self._n)

    async def answer_callback(self, cq_id, text=None):
        self.acks.append((cq_id, text))


def fake_agent(urgency: int, category: str = "person") -> AgentClient:
    def handle(payload):
        assert payload["task"] == "triage"
        assert "payload" in payload["observation"]
        return {"task": "triage", "result": TriageResult(urgency=urgency, category=category, reason="fake").model_dump(),
                "model_id": "fake-model", "latency_ms": 1}
    return AgentClient("local", handle_fn=handle)


def gmail_obs(key: str, sender="Mara <mara@example-client.test>") -> Observation:
    return Observation(user_id=USER_ID, source="gmail", source_key=key, kind="message_in",
                       occurred_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC), thread_key=f"th-{key}",
                       payload={"from": sender, "subject": f"subject {key}", "snippet": "hello there"})


async def test_triage_worker_notifies_and_records(conn, settings):
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)
    stored = await observation_repo.insert(conn, gmail_obs("a"))
    await triage.handle(stored, settings=settings, agent=fake_agent(4), notifier=notifier)

    row = await budget_repo.get_triage(conn, stored.id)
    assert row["urgency"] == 4 and row["model_id"] == "fake-model" and row["latency_ms"] == 1
    assert len(tg.sent) == 1 and tg.sent[0]["chat"] == "777"
    assert "subject a" in tg.sent[0]["text"] and "❗" in tg.sent[0]["text"]
    buttons = tg.sent[0]["markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["👍 useful", "👎 noise", "🔇 mute thread"]
    cur = await conn.execute("select id, tg_message_id, thread_key, urgency from sent_notification")
    sent = await cur.fetchone()
    assert sent["tg_message_id"] == 101 and sent["thread_key"] == "th-a" and sent["urgency"] == 4
    assert buttons[0]["callback_data"] == f"fb:useful:{sent['id']}"
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)
    # sender identity was resolved
    cur = await conn.execute("select value from identity where kind='email'")
    assert (await cur.fetchone())["value"] == "mara@example-client.test"


async def test_triage_worker_silent_when_gate_blocks(conn, settings):
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)
    stored = await observation_repo.insert(conn, gmail_obs("low"))
    await triage.handle(stored, settings=settings, agent=fake_agent(2, "newsletter"), notifier=notifier)
    assert tg.sent == []
    assert (await budget_repo.get_triage(conn, stored.id))["urgency"] == 2   # triage recorded anyway
    cur = await conn.execute("select count(*) as n from sent_notification")
    assert (await cur.fetchone())["n"] == 0


async def test_feedback_roundtrip(conn, settings):
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)
    stored = await observation_repo.insert(conn, gmail_obs("fb"))
    await triage.handle(stored, settings=settings, agent=fake_agent(4), notifier=notifier)
    sent_id = tg.sent[0]["markup"]["inline_keyboard"][0][1]["callback_data"].split(":")[2]

    def cb(data: str, key: str) -> Observation:
        update = {"update_id": int(key), "callback_query": {"id": f"cq{key}", "data": data,
                  "message": {"message_id": 101, "chat": {"id": 777}}}}
        return Observation(user_id=USER_ID, source="telegram", source_key=key, kind="message_in",
                           occurred_at=datetime.now(tz=UTC), thread_key="777", payload=update)

    noise = await observation_repo.insert(conn, cb(f"fb:noise:{sent_id}", "1"))
    await triage.handle(noise, settings=settings, agent=fake_agent(5), notifier=notifier)
    cur = await conn.execute("select user_feedback from sent_notification where id = %s", (sent_id,))
    assert (await cur.fetchone())["user_feedback"] == "noise"
    assert tg.acks[-1] == ("cq1", "Noted: noise 👎")

    mute = await observation_repo.insert(conn, cb(f"mute:thread:{sent_id}", "2"))
    await triage.handle(mute, settings=settings, agent=fake_agent(5), notifier=notifier)
    cur = await conn.execute("select kind, value from mute_rule")
    assert [(r["kind"], r["value"]) for r in await cur.fetchall()] == [("thread", "th-fb")]
    assert tg.acks[-1][1] == "Thread muted 🔇"
    assert len(tg.sent) == 1  # control-channel observations never get triaged/notified

    # a follow-up in the muted thread, even urgency 5, is silent
    again = await observation_repo.insert(conn, Observation(**{**gmail_obs("fb2").model_dump(), "thread_key": "th-fb"}))
    await triage.handle(again, settings=settings, agent=fake_agent(5), notifier=notifier)
    assert len(tg.sent) == 1

    # plain text from the control channel is ignored in phase 1 (chat comes in phase 3)
    text = await observation_repo.insert(conn, Observation(user_id=USER_ID, source="telegram", source_key="9", kind="message_in",
                                                           occurred_at=datetime.now(tz=UTC), thread_key="777",
                                                           payload={"update_id": 9, "message": {"text": "hi", "chat": {"id": 777}}}))
    await triage.handle(text, settings=settings, agent=fake_agent(5), notifier=notifier)
    assert len(tg.sent) == 1


def test_render_is_source_agnostic():
    obs = Observation(user_id=USER_ID, source="calendar", source_key="e1:2026", kind="event_starting",
                      occurred_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC), thread_key="e1",
                      payload={"summary": "Standup", "location": "Room B"})
    text = triage.Notifier.render(obs, TriageResult(urgency=5, category="calendar", reason="starts soon",
                                                     summary="Standup with the client team starts in 15 minutes in Room B."))
    assert text.startswith("‼️ Standup · calendar") and "starts in 15 minutes" in text and "starts soon" not in text
    assert json.dumps(triage.Notifier.buttons(UUID_ZERO := "00000000-0000-0000-0000-000000000000")).count(UUID_ZERO) == 3


async def test_own_sent_mail_is_not_triaged_and_teaches_own_address(conn, settings):
    """A Gmail message with the SENT label is the person's own reply: no triage row, no notification — and its
    sender is remembered as one of the person's own addresses (so prompts can tell their mail from others')."""
    from core.repo import user_repo

    await user_repo.create(conn, control_source="telegram", control_thread_key="777", user_id=USER_ID, display_name="Aşkın Kadir Çekim")
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "777", USER_ID)
    own = gmail_obs("sent1", sender='"Aşkın Kadir Çekim" <KadirCekim.07@gmail.com>')
    own.kind = "message_out"
    own.payload["label_ids"] = ["SENT"]
    stored = await observation_repo.insert(conn, own)

    def never(payload):
        raise AssertionError("the person's own mail must not be triaged")
    await triage.handle(stored, settings=settings, agent=AgentClient("local", handle_fn=never), notifier=notifier)
    assert await budget_repo.get_triage(conn, stored.id) is None and tg.sent == []
    assert user_repo.own_emails(await user_repo.get(conn, USER_ID)) == ["kadircekim.07@gmail.com"]
    await triage.handle(stored, settings=settings, agent=AgentClient("local", handle_fn=never), notifier=notifier)
    assert user_repo.own_emails(await user_repo.get(conn, USER_ID)) == ["kadircekim.07@gmail.com"]   # deduplicated

    # the triage prompt for a real incoming mail now carries the name and the person's own addresses
    seen = {}
    def capture(payload):
        seen.update(payload["observation"]["user"])
        return {"task": "triage", "result": TriageResult(urgency=2, category="person", reason="fake").model_dump(), "model_id": "m", "latency_ms": 1}
    await triage.handle(await observation_repo.insert(conn, gmail_obs("in1")), settings=settings, agent=AgentClient("local", handle_fn=capture), notifier=notifier)
    assert seen["display_name"] == "Aşkın Kadir Çekim" and seen["emails"] == ["kadircekim.07@gmail.com"]
