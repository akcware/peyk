from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.models import Connection, Observation
from core.repo import action_repo
from tests.conftest import USER_ID
from tests.phase1.test_worker_flow import FakeTelegram
from workers import actions


class FakeGmail:
    id = "composio"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    def capabilities(self):
        return {"can_send": True, "needs_session": False, "needs_user_device": False}

    async def connect(self, user_id):
        return Connection(adapter_id=self.id, user_id=user_id)

    async def send(self, conn, thread_key, content):
        if self.fail:
            raise RuntimeError("composio down")
        self.sent.append({"thread_key": thread_key, "body": content.text, "subject": content.subject, "to": content.to})
        return f"gmail-msg-{len(self.sent)}"


def test_hash_canonical():
    a = {"body": "hi", "to": ["a@x.test"], "subject": None}
    b = {"subject": None, "to": ["a@x.test"], "body": "hi"}
    assert action_repo.content_hash(a) == action_repo.content_hash(b)
    assert action_repo.content_hash({**a, "body": "hi!"}) != action_repo.content_hash(a)
    assert action_repo.canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert len(action_repo.content_hash(a)) == 64


async def test_pally_bug(conn):
    """draft v1 -> present -> edit v2 -> approve(hash_v1) is rejected; approve(hash_v2) -> send delivers v2."""
    gmail = FakeGmail()
    handle = await gmail.connect(USER_ID)
    a = await action_repo.create(conn, USER_ID, channel="gmail", thread_key="th1", content={"body": "v1", "to": [], "subject": None})
    h1 = a["content_hash"]
    a = await actions.present(conn, a["id"])
    assert a["status"] == "awaiting_approval"
    a = await actions.edit(conn, a["id"], {**a["content"], "body": "v2"})
    h2 = a["content_hash"]
    assert h1 != h2 and a["status"] == "awaiting_approval" and a["approved_hash"] is None

    with pytest.raises(actions.ApprovalMismatch):
        await actions.approve(conn, a["id"], h1)
    with pytest.raises(actions.ApprovalMismatch):
        await actions.approve(conn, a["id"], h1[:16])
    a = await action_repo.get(conn, a["id"])
    assert a["status"] == "awaiting_approval" and gmail.sent == []
    with pytest.raises(actions.InvalidTransition):
        await actions.send(conn, a["id"], gmail, handle)
    assert gmail.sent == []

    a = await actions.approve(conn, a["id"], h2[:16])
    assert a["status"] == "approved" and a["approved_hash"] == h2
    a = await actions.send(conn, a["id"], gmail, handle)
    assert a["status"] == "sent" and a["external_id"] == "gmail-msg-1" and a["sent_at"] is not None
    assert gmail.sent == [{"thread_key": "th1", "body": "v2", "subject": None, "to": []}]


async def test_send_idempotent(conn):
    gmail = FakeGmail()
    handle = await gmail.connect(USER_ID)
    a = await action_repo.create(conn, USER_ID, channel="gmail", thread_key=None, content={"body": "x", "to": ["a@x.test"], "subject": "s"})
    await actions.present(conn, a["id"])
    await actions.approve(conn, a["id"], a["content_hash"])
    await actions.send(conn, a["id"], gmail, handle)
    again = await actions.send(conn, a["id"], gmail, handle)
    assert len(gmail.sent) == 1 and again["status"] == "sent"


async def test_send_requires_approved(conn):
    gmail = FakeGmail()
    handle = await gmail.connect(USER_ID)
    a = await action_repo.create(conn, USER_ID, channel="gmail", thread_key=None, content={"body": "x", "to": ["a@x.test"], "subject": "s"})
    with pytest.raises(actions.InvalidTransition):
        await actions.send(conn, a["id"], gmail, handle)      # draft
    await actions.present(conn, a["id"])
    with pytest.raises(actions.InvalidTransition):
        await actions.send(conn, a["id"], gmail, handle)      # awaiting_approval
    assert gmail.sent == []
    await actions.reject(conn, a["id"])
    with pytest.raises(actions.InvalidTransition):
        await actions.approve(conn, a["id"], a["content_hash"])   # rejected drafts cannot be approved


async def test_callback_hash_prefix_collision(conn):
    """A 16-hex prefix that matches must still correspond to the full hash; a wrong prefix or a prefix shorter than 16 is rejected."""
    a = await action_repo.create(conn, USER_ID, channel="gmail", thread_key=None, content={"body": "x", "to": ["a@x.test"], "subject": None})
    await actions.present(conn, a["id"])
    full = a["content_hash"]
    with pytest.raises(actions.ApprovalMismatch):
        await actions.approve(conn, a["id"], full[:15])            # too short
    with pytest.raises(actions.ApprovalMismatch):
        await actions.approve(conn, a["id"], full[:16][::-1])      # wrong
    with pytest.raises(actions.ApprovalMismatch):
        await actions.approve(conn, a["id"], full[:16] + "zz")     # not a prefix of the full hash
    approved = await actions.approve(conn, a["id"], full[:16])
    assert approved["approved_hash"] == full                        # stored as the full hash, never the prefix
    parsed = actions.parse_callback(f"act:approve:{a['id'].hex}:{full[:16]}")
    assert parsed == ("approve", a["id"], full[:16])
    assert actions.parse_callback("act:approve:not-a-uuid:abc") is None and actions.parse_callback("fb:useful:x") is None


async def test_failed_send_keeps_approval_and_retries(conn):
    flaky = FakeGmail(fail=True)
    handle = await flaky.connect(USER_ID)
    a = await action_repo.create(conn, USER_ID, channel="gmail", thread_key="t", content={"body": "x", "to": [], "subject": None})
    await actions.present(conn, a["id"])
    await actions.approve(conn, a["id"], a["content_hash"])
    with pytest.raises(RuntimeError):
        await actions.send(conn, a["id"], flaky, handle)
    a = await action_repo.get(conn, a["id"])
    assert a["status"] == "failed" and a["approved_hash"] == a["content_hash"]
    flaky.fail = False
    a = await actions.retry(conn, a["id"], flaky, handle)
    assert a["status"] == "sent" and len(flaky.sent) == 1


def _cb(data: str, key: str, tg_mid: int = 500) -> Observation:
    return Observation(user_id=USER_ID, source="telegram", source_key=key, kind="message_in", occurred_at=datetime.now(tz=UTC),
                       thread_key="777", payload={"update_id": int(key), "callback_query": {"id": f"cq{key}", "data": data,
                                                  "message": {"message_id": tg_mid, "chat": {"id": 777}}}})


def _txt(text: str, key: str) -> Observation:
    return Observation(user_id=USER_ID, source="telegram", source_key=key, kind="message_in", occurred_at=datetime.now(tz=UTC),
                       thread_key="777", payload={"update_id": int(key), "message": {"text": text, "chat": {"id": 777}}})


async def test_telegram_flow_draft_edit_approve_old_button(conn, settings):
    from core.adapter import AdapterRegistry
    from core.repo import observation_repo
    from workers import approval, triage

    gmail = FakeGmail()
    tg = FakeTelegram()
    tg.edits = []
    async def edit_message(chat_id, message_id, text, reply_markup=None):
        tg.edits.append({"message_id": message_id, "text": text, "markup": reply_markup})
    tg.edit_message = edit_message
    registry = AdapterRegistry({"composio": gmail, "telegram": tg})
    notifier = triage.Notifier(tg, "777", USER_ID)
    flow = approval.ApprovalFlow(registry, notifier)

    # agent produced a draft
    src = await observation_repo.insert(conn, _txt("write to mara that we move the meeting to tomorrow", "1"))
    action = await flow.on_draft(conn, src, {"intent": "ActionDraft", "channel": "gmail", "thread_key": "th-mara",
                                              "body": "Hi Mara, can we move the meeting to tomorrow?", "to": [], "subject": None})
    assert action["status"] == "awaiting_approval"
    draft_msg = tg.sent[-1]
    old_buttons = draft_msg["markup"]["inline_keyboard"][0]
    assert [b["text"] for b in old_buttons] == ["✅ Send", "✏️ Edit", "❌ Cancel"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in old_buttons)

    # Edit -> next message is the correction -> re-presented with a new hash
    edit_cb = await observation_repo.insert(conn, _cb(old_buttons[1]["callback_data"], "2", draft_msg["message_id"]))
    await triage.handle(edit_cb, settings=settings, agent=None, notifier=notifier, approval=flow)
    assert "corrected text" in tg.sent[-1]["text"]
    fix = await observation_repo.insert(conn, _txt("Hi Mara, can we move the meeting to tomorrow at 15:00?", "3"))
    await triage.handle(fix, settings=settings, agent=None, notifier=notifier, approval=flow)
    new_msg = tg.sent[-1]
    new_buttons = new_msg["markup"]["inline_keyboard"][0]
    assert new_buttons[0]["callback_data"] != old_buttons[0]["callback_data"]
    assert "15:00" in new_msg["text"]

    # Old message's Send button -> rejected, nothing sent, draft re-presented
    old_send = await observation_repo.insert(conn, _cb(old_buttons[0]["callback_data"], "4", draft_msg["message_id"]))
    await triage.handle(old_send, settings=settings, agent=None, notifier=notifier, approval=flow)
    assert gmail.sent == [] and tg.acks[-1][1].startswith("The draft changed")
    assert (await action_repo.get(conn, action["id"]))["status"] == "awaiting_approval"

    # New Send -> sent with v2, Telegram message edited
    new_send = await observation_repo.insert(conn, _cb(new_buttons[0]["callback_data"], "5", new_msg["message_id"]))
    await triage.handle(new_send, settings=settings, agent=None, notifier=notifier, approval=flow)
    assert len(gmail.sent) == 1 and gmail.sent[0]["body"].endswith("15:00?") and gmail.sent[0]["thread_key"] == "th-mara"
    final = await action_repo.get(conn, action["id"])
    assert final["status"] == "sent" and final["external_id"] == "gmail-msg-1"
    assert tg.edits[-1]["message_id"] == new_msg["message_id"] and tg.edits[-1]["text"].startswith("Sent 👍")

    # pressing Send again on the sent draft: idempotent, no second mail
    again = await observation_repo.insert(conn, _cb(new_buttons[0]["callback_data"], "6", new_msg["message_id"]))
    await triage.handle(again, settings=settings, agent=None, notifier=notifier, approval=flow)
    assert len(gmail.sent) == 1

    # 'edit:' prefix without pending state targets the newest open draft
    b = await flow.on_draft(conn, src, {"intent": "ActionDraft", "channel": "gmail", "body": "second", "to": ["z@x.test"], "subject": "S"})
    pre = await observation_repo.insert(conn, _txt("edit: second, revised", "7"))
    await triage.handle(pre, settings=settings, agent=None, notifier=notifier, approval=flow)
    assert (await action_repo.get(conn, b["id"]))["content"]["body"] == "second, revised"
    # Cancel
    cancel = await observation_repo.insert(conn, _cb(tg.sent[-1]["markup"]["inline_keyboard"][0][2]["callback_data"], "8", tg.sent[-1]["message_id"]))
    await triage.handle(cancel, settings=settings, agent=None, notifier=notifier, approval=flow)
    assert (await action_repo.get(conn, b["id"]))["status"] == "rejected"


async def test_draft_card_is_plain_and_localized(conn):
    """The approval card carries no id/hash footer, and card + buttons follow the person's language."""
    a = await action_repo.create(conn, USER_ID, channel="gmail", thread_key=None,
                                 content={"body": "Merhaba Nezir", "to": ["nezir@example.test"], "subject": "Fatura"})
    en = actions.render_draft(a)
    assert en == "📝 Draft\nTo: nezir@example.test\nSubject: Fatura\n\nMerhaba Nezir"
    assert str(a["id"])[:8] not in en and a["content_hash"][:8] not in en
    tr = actions.render_draft(a, "tr")
    assert tr.startswith("📝 Taslak\nKime: nezir@example.test\nKonu: Fatura")
    assert [b["text"] for b in actions.buttons(a, "tr")["inline_keyboard"][0]] == ["✅ Gönder", "✏️ Düzelt", "❌ Vazgeç"]
    assert [b["text"] for b in actions.buttons(a)["inline_keyboard"][0]] == ["✅ Send", "✏️ Edit", "❌ Cancel"]
    assert actions.buttons(a, "tr")["inline_keyboard"][0][0]["callback_data"] == actions.buttons(a)["inline_keyboard"][0][0]["callback_data"]


def test_phrases_fall_back_to_english():
    from core.phrases import TEXTS, phrase

    assert phrase("tr", "sent") == "Gönderdim 👍" and phrase("de", "sent") == "Gesendet 👍"
    assert phrase(None, "sent") == "Sent 👍" and phrase("xx", "sent") == "Sent 👍" and phrase("tr-TR", "sent") == "Gönderdim 👍"
    assert phrase("tr", "send_failed").startswith("Gönderemedim.")
    assert set(TEXTS["tr"]) == set(TEXTS["en"]) == set(TEXTS["de"])          # every language covers every key
    assert not any("—" in v for v in TEXTS["tr"].values() if not v.startswith("Taslak —"))
