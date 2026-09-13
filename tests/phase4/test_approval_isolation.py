"""A card reaches only the person who asked for it, and only its owner can press it. On 2026-09-14 the shared flow
held the notifier of whoever's observation had started last (consumers run in parallel), and a person's calendar
card and mail draft went to another person's chat. What a button did is part of the conversation afterwards."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent.client import AgentClient
from core.adapter import AdapterRegistry
from core.embeddings import FakeEmbedder
from core.models import Observation
from core.phrases import phrase
from core.repo import action_repo, observation_repo
from tests.conftest import USER_ID
from tests.phase1.test_worker_flow import FakeTelegram
from tests.phase4.test_approval_flow import (
    FakeMailAdapter,
    FakeNotifier,
    _approve_obs,
    _draft_and_approve,
    _draft_obs,
)
from workers import chat, triage
from workers.approval import ApprovalFlow


def _tg(user_id, chat_id: str, key: str, text: str) -> Observation:
    return Observation(user_id=user_id, source="telegram", source_key=key, kind="message_in", occurred_at=datetime.now(tz=UTC),
                       thread_key=chat_id, payload={"update_id": int(key), "message": {"text": text, "chat": {"id": int(chat_id)}}})


async def test_card_goes_to_the_asking_persons_chat_even_if_another_turn_starts(conn, settings):
    flow = ApprovalFlow(AdapterRegistry({"composio": FakeMailAdapter()}), None)
    mine, theirs = FakeTelegram(), FakeTelegram()
    other_person = triage.Notifier(theirs, "222", uuid4())

    def agent(payload):
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": ""}
        flow.notifier = other_person           # another person's observation starts while this turn runs
        return {"task": "chat", "reply": "Taslak hazır.", "intents": [
            {"intent": "ActionDraft", "channel": "gmail", "to": ["ekin@example.test"], "subject": "Toplantı", "body": "Merhaba"}]}

    msg = await observation_repo.insert(conn, _tg(USER_ID, "111", "901", "Ekin'e mail yaz"))
    await triage.handle(msg, settings=settings, agent=AgentClient("local", handle_fn=agent),
                        notifier=triage.Notifier(mine, "111", USER_ID), embedder=FakeEmbedder(), approval=flow)
    assert theirs.sent == []                                                  # nothing reached the other chat
    assert [m["chat"] for m in mine.sent] == ["111", "111"] and "Merhaba" in mine.sent[-1]["text"]   # reply, then the card


async def test_nobody_else_can_press_your_card(conn):
    adapter = FakeMailAdapter()
    flow = ApprovalFlow(AdapterRegistry({"composio": adapter}), None)
    alice, bob = uuid4(), uuid4()
    card = await flow.with_notifier(FakeNotifier()).on_draft(conn, _draft_obs(alice, "111"),
                                                             {"body": "hi", "to": ["ekin@example.test"], "channel": "gmail"})
    pressed = await flow.with_notifier(FakeNotifier()).on_callback(conn, _approve_obs(bob, "222", card, 101))
    assert pressed == phrase("tr", "card_not_yours") and adapter.sends == []
    assert (await action_repo.get(conn, card["id"]))["status"] == "awaiting_approval"


async def test_what_a_button_did_is_in_the_chat_history(conn):
    flow = ApprovalFlow(AdapterRegistry({"composio": FakeMailAdapter()}), None)
    alice = uuid4()
    await _draft_and_approve(conn, flow, alice, "111", FakeNotifier(), "thread-a")
    later = _tg(alice, "111", "950", "olur yaz")
    later.occurred_at = datetime.now(tz=UTC) + timedelta(seconds=1)
    history = await chat.build_history(conn, later)
    assert history[-1]["role"] == "assistant" and "pressed a button on my card: sent" in history[-1]["text"]
