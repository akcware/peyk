from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from adapters.composio.adapter import ComposioAdapter
from adapters.composio.mappings import MAPPINGS, get_path, parse_timestamp
from adapters.composio.webhook import parse_envelope, to_observation
from core.adapter import AdapterRegistry, validate_capabilities
from core.models import Connection
from tests.conftest import USER_ID

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def _event() -> dict:
    return json.loads((FIX / "composio_events" / "gmail_new_message.json").read_text())


def test_composio_mapping_parses_fixture():
    obs = to_observation(parse_envelope(_event()), USER_ID)
    assert obs is not None
    assert obs.source == "gmail" and obs.kind == "message_in"
    assert obs.source_key == "19b11732c1b578fd"          # Gmail message id
    assert not obs.source_key.startswith("msg_")          # NOT Composio's envelope id
    assert obs.thread_key == "19b11732c1b578fd"
    assert obs.occurred_at == datetime(2026, 9, 10, 9, 13, 52, tzinfo=UTC)
    assert obs.payload["from"].endswith("<mara@example-client.test>")
    assert obs.payload["subject"].startswith("Invoice #2041")
    assert obs.payload["snippet"].startswith("Hi, just a reminder")
    assert obs.payload["label_ids"] == ["INBOX", "UNREAD", "IMPORTANT"]
    assert obs.is_backfill is False


def test_unknown_trigger_dropped():
    ev = _event()
    ev["metadata"]["trigger_slug"] = "SLACK_SOMETHING_NEW"
    assert to_observation(parse_envelope(ev), USER_ID) is None
    assert to_observation({"trigger_slug": "", "payload": {}}, USER_ID) is None
    assert to_observation({"trigger_slug": "GMAIL_NEW_GMAIL_MESSAGE", "payload": {}}, USER_ID) is None  # no id


def test_ws_and_webhook_same_observation():
    """The SDK's realtime path builds a TriggerEvent from the same V3 envelope; both must map identically."""
    from composio.core.models.triggers import _build_trigger_event_from_v3

    ws_event = _build_trigger_event_from_v3(_event())
    webhook_event = parse_envelope(json.dumps(_event()))
    a = to_observation(dict(ws_event), USER_ID)
    b = to_observation(webhook_event, USER_ID)
    assert a is not None and b is not None
    assert a.model_dump(exclude={"id", "received_at"}) == b.model_dump(exclude={"id", "received_at"})


def test_legacy_envelope_still_parses():
    legacy = {"appName": "gmail", "payload": _event()["data"],
              "metadata": {"triggerName": "GMAIL_NEW_GMAIL_MESSAGE", "nanoId": "x"}}
    obs = to_observation(parse_envelope(legacy), USER_ID)
    assert obs is not None and obs.source_key == "19b11732c1b578fd"


def test_path_and_timestamp_helpers():
    assert get_path({"a": {"b": [1, {"c": 2}]}}, "$.a.b.1.c") == 2
    assert get_path({"a": None}, "$.a.b") is None
    assert parse_timestamp("2026-09-10T09:13:52Z") == datetime(2026, 9, 10, 9, 13, 52, tzinfo=UTC)
    assert parse_timestamp(1789031632000) == datetime(2026, 9, 10, 9, 13, 52, tzinfo=UTC)
    assert parse_timestamp("garbage") is None


async def test_backfill_via_action(settings):
    pages = {None: json.loads((FIX / "gmail_messages" / "fetch_emails_page1.json").read_text()),
             "page-2-token": json.loads((FIX / "gmail_messages" / "fetch_emails_page2.json").read_text())}
    calls: list[dict] = []

    def fake_execute(slug, arguments, *, user_id):
        calls.append({"slug": slug, "args": arguments, "user_id": user_id})
        return pages[arguments.get("page_token")]

    adapter = ComposioAdapter(settings=settings, execute=fake_execute)
    conn = Connection(adapter_id="composio", user_id=USER_ID)
    since = datetime(2026, 9, 1, tzinfo=UTC)
    out = [o async for o in adapter.backfill(conn, since)]
    assert len(calls) == 2 and calls[0]["slug"] == "GMAIL_FETCH_EMAILS"
    assert calls[0]["args"]["query"] == f"after:{int(since.timestamp())}" and "page_token" not in calls[0]["args"]
    assert calls[1]["args"]["page_token"] == "page-2-token"
    assert [o.source_key for o in out] == ["19b1170000000001", "19b1170000000002", "19b1170000000003"]
    assert out[2].thread_key == "19b1170000000001"
    assert out[0].payload["from"].startswith("Newsletter Bot")
    assert all(o.source == "gmail" and o.kind == "message_in" for o in out)


async def test_backfill_flag(settings, conn):
    from core.repo import observation_repo

    page = json.loads((FIX / "gmail_messages" / "fetch_emails_page2.json").read_text())
    adapter = ComposioAdapter(settings=settings, execute=lambda slug, args, *, user_id: page)
    c = Connection(adapter_id="composio", user_id=USER_ID)
    out = [o async for o in adapter.backfill(c, datetime(2026, 9, 1, tzinfo=UTC))]
    assert out and all(o.is_backfill for o in out)
    stored = await observation_repo.insert(conn, out[0])
    assert stored.is_backfill is True
    assert stored.occurred_at != stored.received_at
    assert stored.occurred_at == datetime(2026, 9, 9, 11, 45, tzinfo=UTC)
    # reconcile (phase 2) reuses the same path with is_backfill=False
    fresh = [o async for o in adapter.backfill(c, datetime(2026, 9, 1, tzinfo=UTC), is_backfill=False)]
    assert all(not o.is_backfill for o in fresh)


async def test_subscribe_noop_in_webhook_mode(settings):
    s = settings.model_copy(update={"COMPOSIO_DELIVERY": "webhook"})
    adapter = ComposioAdapter(settings=s)
    got = [o async for o in adapter.subscribe(Connection(adapter_id="composio", user_id=USER_ID))]
    assert got == []


def test_adapter_capabilities(settings):
    registry = AdapterRegistry.from_ids(
        ["composio", "telegram", "whatsapp"],
        composio={"settings": settings}, telegram={"settings": settings},
    )
    for adapter in registry.all():
        caps = validate_capabilities(adapter.capabilities())
        assert set(caps) == {"can_send", "needs_session", "needs_user_device"}
    assert registry.get("composio").capabilities() == {"can_send": True, "needs_session": False, "needs_user_device": False}
    assert registry.get("telegram").capabilities() == {"can_send": True, "needs_session": False, "needs_user_device": False}
    # routing by data: session adapters get no ingest task
    assert {a.id for a in registry.ingestable()} == {"composio", "telegram"}
    assert {a.id for a in registry.senders()} == {"composio", "telegram", "whatsapp"}


def test_mapping_table_shape():
    for slug, m in MAPPINGS.items():
        assert slug == slug.upper()
        assert m.source_key.startswith("$.") and m.occurred_at.startswith("$.")


def test_sent_mail_is_the_persons_own_message():
    """Gmail fires the trigger for mail the person sent too. That is their reply, not someone writing to them."""
    ev = _event()
    ev["data"]["label_ids"] = ["SENT"]
    obs = to_observation(parse_envelope(ev), USER_ID)
    assert obs is not None and obs.kind == "message_out" and obs.payload["label_ids"] == ["SENT"]
    ev["data"]["label_ids"] = ["INBOX", "SENT"]           # self-addressed mail: still something they wrote
    assert to_observation(parse_envelope(ev), USER_ID).kind == "message_out"
