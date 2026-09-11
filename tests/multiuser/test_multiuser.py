"""Multi-user + agent-driven onboarding. No LLM: scripted agent, fake adapters, real DB."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx

from adapters.composio.adapter import ComposioAdapter
from adapters.telegram.adapter import TelegramAdapter
from agent.client import AgentClient
from agent.schemas import TriageResult
from core.adapter import AdapterRegistry
from core.embeddings import FakeEmbedder
from core.models import Choice, Connection, Content, Observation
from core.repo import job_repo, observation_repo, user_repo
from tests.conftest import USER_ID
from tests.phase1.test_worker_flow import FakeTelegram, fake_agent
from workers import account, scheduler, ticks, triage, users


class FakeComposio:
    id = "composio"

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, str]] = {}      # composio_user_id -> {toolkit: account_id}
        self.status: dict[str, str] = {}                    # connection_id -> status
        self.links: list[tuple[str, str]] = []
        self.triggers: list[tuple[str, str, str]] = []

    def capabilities(self):
        return {"can_send": True, "needs_session": False, "needs_user_device": False}

    async def connect(self, user_id):
        return Connection(adapter_id=self.id, user_id=user_id, data={"composio_user_id": str(user_id)})

    async def connected_toolkits(self, conn):
        return dict(self.accounts.get(conn.data["composio_user_id"], {}))

    async def link(self, conn, toolkit):
        cid = f"conn_{toolkit}_{conn.data['composio_user_id'][:8]}"
        self.links.append((conn.data["composio_user_id"], toolkit))
        self.status[cid] = "INITIATED"
        return {"url": f"https://connect.example.test/{cid}", "connection_id": cid}

    async def connection_status(self, conn, connection_id):
        return self.status.get(connection_id, "")

    async def enable_triggers(self, conn, toolkit, account_id):
        self.triggers.append((conn.data["composio_user_id"], toolkit, account_id))
        return ["ti_fake"]


def tg_text(chat: str, key: str, text: str, user_id: UUID) -> Observation:
    return Observation(user_id=user_id, source="telegram", source_key=key, kind="message_in", occurred_at=datetime.now(tz=UTC),
                       thread_key=chat, payload={"update_id": int(key), "message": {"text": text, "chat": {"id": int(chat)},
                                                                                    "from": {"first_name": "Deniz"}}})


async def test_two_users_isolated(conn, settings):
    a = await user_repo.create(conn, control_source="telegram", control_thread_key="111", display_name="A", language="tr")
    b = await user_repo.create(conn, control_source="telegram", control_thread_key="222", display_name="B", language="en")
    assert a["composio_user_id"] == str(a["id"]) and b["composio_user_id"] == str(b["id"])
    tg = FakeTelegram()
    registry = AdapterRegistry({"telegram": tg})
    notifiers = users.Notifiers(registry, settings)
    mail_for_b = await observation_repo.insert(conn, Observation(user_id=b["id"], source="gmail", source_key="mb", kind="message_in",
                                                                  occurred_at=datetime.now(tz=UTC), thread_key="t", payload={"from": "X <x@y.test>", "subject": "for B"}))
    await triage.handle(mail_for_b, settings=settings, agent=fake_agent(4), notifiers=notifiers)
    assert len(tg.sent) == 1 and tg.sent[0]["chat"] == "222"
    # A's queue state and budget are untouched
    cur = await conn.execute("select user_id from sent_notification"); rows = await cur.fetchall()
    assert [r["user_id"] for r in rows] == [b["id"]]
    # the notification record lives in B's control thread only
    outs = await observation_repo.list_by_thread(conn, b["id"], "222", limit=5)
    assert outs and outs[0].payload["kind"] == "notification"
    assert await observation_repo.list_by_thread(conn, a["id"], "111", limit=5) == []


async def test_telegram_auto_registers_and_composio_maps_events(conn, settings):
    directory = users.DbUserDirectory(settings)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if request.url.path.endswith("getUpdates"):
            if payload.get("offset"):
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 50, "message": {"message_id": 1, "date": 1, "chat": {"id": 555}, "from": {"first_name": "Yeni", "last_name": "Kişi"}, "text": "selam"}}]})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    s = settings.model_copy(update={"TELEGRAM_BOT_TOKEN": "t"})
    adapter = TelegramAdapter(settings=s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org/bott"),
                              poll_timeout=0, users=directory)
    got = []
    async for o in adapter.subscribe(Connection(adapter_id="telegram", user_id=USER_ID)):
        got.append(o)
        break
    user = await user_repo.get_by_control(conn, "telegram", "555")
    assert user is not None and got[0].user_id == user["id"] and user["display_name"] == "Yeni Kişi"
    assert user["composio_user_id"] == str(user["id"])

    # Composio events: our uuid -> that user; legacy entity id -> bootstrap user; unknown -> dropped
    comp = ComposioAdapter(settings=settings, users=directory)
    assert await comp.user_for_event({"user_id": str(user["id"])}, default=USER_ID) == user["id"]
    assert await comp.user_for_event({"user_id": settings.COMPOSIO_USER_ID}, default=USER_ID) == USER_ID
    assert await comp.user_for_event({"user_id": "somebody-else"}, default=USER_ID) is None

    # choices render as an inline keyboard on Telegram
    await adapter.send(Connection(adapter_id="telegram", user_id=USER_ID), "555",
                       Content(text="pick", choices=[Choice(text="A", data="x:a"), Choice(text="B", data="x:b")]))
    assert seen[-1]["reply_markup"] == {"inline_keyboard": [[{"text": "A", "callback_data": "x:a"}, {"text": "B", "callback_data": "x:b"}]]}


async def test_agent_driven_onboarding_end_to_end(conn, settings):
    """New person writes -> full agent runs (no ack) -> agent greets + connect_service(gmail) -> link sent ->
    poll job -> ACTIVE -> triggers enabled + confirmation; set_profile intent updates the user."""
    user, created = await user_repo.get_or_create_by_control(conn, "telegram", "777", display_name="Deniz")
    assert created
    comp, tg = FakeComposio(), FakeTelegram()
    registry = AdapterRegistry({"composio": comp, "telegram": tg})
    notifier = triage.Notifier(tg, "777", user["id"])
    calls: list[dict] = []

    def handle(payload):
        calls.append(payload)
        if payload["task"] == "chat_ack":
            raise AssertionError("ack stage must be skipped for a new user")
        st = payload["user_state"]
        assert st["is_new"] and st["connected"] == [] and "gmail" in st["available"]
        assert payload["user"]["display_name"] == "Deniz"
        return {"task": "chat", "reply": "Merhaba Deniz! Gmail'ini bağlayalım mı? Link geliyor.", "intents": [
            {"intent": "ConnectRequest", "service": "gmail"},
            {"intent": "ProfileUpdate", "profile": "Karlsruhe'de öğrenci, ev arıyor", "language": "tr", "timezone": "Europe/Berlin", "display_name": ""},
        ]}

    from workers import chat
    msg = await observation_repo.insert(conn, tg_text("777", "1", "selam", user["id"]))
    await chat.handle_message(conn, msg, settings=settings, agent=AgentClient("local", handle_fn=handle), notifier=notifier,
                              embedder=FakeEmbedder(), registry=registry)
    assert [c["task"] for c in calls] == ["chat"]
    texts = [m["text"] for m in tg.sent]
    assert texts[0].startswith("Merhaba Deniz") and "https://connect.example.test/conn_gmail_" in texts[1]
    assert comp.links == [(str(user["id"]), "gmail")]
    u = await user_repo.get(conn, user["id"])
    assert u["profile"].startswith("Karlsruhe") and u["language"] == "tr" and u["timezone"] == "Europe/Berlin" and u["display_name"] == "Deniz"
    assert "gmail" in u["state"]["pending"] and u["state"]["greeted"] is True
    jobs = await job_repo.pending_of_kind(conn, user["id"], "await_connection")
    assert len(jobs) == 1 and jobs[0]["payload"]["toolkit"] == "gmail" and jobs[0]["recurrence"] == "every:20s"

    # second message: no longer "new" -> ack stage runs again
    calls.clear()
    def handle2(payload):
        calls.append(payload["task"])
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": False, "message": "Bekliyorum, linke tıklaman yeterli."}
        return {"task": "chat", "reply": "unused", "intents": []}
    msg2 = await observation_repo.insert(conn, tg_text("777", "2", "tamam", user["id"]))
    await chat.handle_message(conn, msg2, settings=settings, agent=AgentClient("local", handle_fn=handle2), notifier=notifier,
                              embedder=FakeEmbedder(), registry=registry)
    assert calls == ["chat_ack"]

    # poll: still pending -> nothing; then ACTIVE -> triggers + confirmation + job cancelled
    ctx = ticks.TickContext(settings=settings, registry=registry, notifier=notifier)
    fired = await scheduler.fire_due(conn, user["id"], datetime.now(tz=UTC) + timedelta(seconds=30), settings.TIMEZONE)
    assert fired == 1
    tick = next(o for o in await observation_repo.list_since(conn, user["id"], datetime.now(tz=UTC) - timedelta(days=1)) if o.kind == "tick")
    n_before = len(tg.sent)
    await ticks.handle_tick(conn, tick, ctx)
    assert len(tg.sent) == n_before and comp.triggers == []
    cid = jobs[0]["payload"]["connection_id"]
    comp.status[cid] = "ACTIVE"
    comp.accounts[str(user["id"])] = {"gmail": "ca_new"}
    seen_events = []
    def voice(payload):
        if payload["task"] == "chat":
            seen_events.append(payload["message"])
            return {"task": "chat", "reply": "Gmail bağlandı, artık maillerini izliyorum. Önemli bir şey olunca yazarım.", "intents": []}
        return {"task": "chat_ack", "needs_work": True, "message": ""}
    ctx.agent, ctx.embedder = AgentClient("local", handle_fn=voice), FakeEmbedder()
    await ticks.handle_tick(conn, tick, ctx)
    assert comp.triggers == [(str(user["id"]), "gmail", "ca_new")]
    assert seen_events and seen_events[0].startswith("[system event: Gmail was just connected")
    assert tg.sent[-1]["text"] == "Gmail bağlandı, artık maillerini izliyorum. Önemli bir şey olunca yazarım."
    outs = [o for o in await observation_repo.list_by_thread(conn, user["id"], "777", limit=5) if o.kind == "message_out"]
    assert outs[0].payload["kind"] == "event_reaction"
    u = await user_repo.get(conn, user["id"])
    assert u["state"]["connected"] == {"gmail": "ca_new"} and u["state"]["pending"] == {}
    assert await job_repo.pending_of_kind(conn, user["id"], "await_connection") == []
    # onboarding state now reports the service and the agent is not "new" anymore
    from workers import onboarding
    st = await onboarding.user_state(conn, u, registry)
    assert st["connected"] == ["gmail"] and not st["is_new"]


async def test_bootstrap_user_from_env_and_default_jobs_per_user(conn, settings):
    s = settings.model_copy(update={"TELEGRAM_CHAT_ID": "999", "USER_PROFILE": "me", "USER_LANGUAGE": "tr"})
    row = await user_repo.ensure_bootstrap(conn, user_id=s.USER_ID, control_source="telegram", control_thread_key="999",
                                           composio_user_id=s.COMPOSIO_USER_ID, profile="me", language="tr", timezone="Europe/Berlin")
    assert row["id"] == s.USER_ID and row["composio_user_id"] == s.COMPOSIO_USER_ID
    again = await user_repo.ensure_bootstrap(conn, user_id=s.USER_ID, control_source="telegram", control_thread_key="999",
                                             composio_user_id=s.COMPOSIO_USER_ID, profile="me", language="tr", timezone="Europe/Berlin")
    assert again["id"] == row["id"]
    other = await user_repo.create(conn, control_source="telegram", control_thread_key="1000", timezone="America/New_York")
    await scheduler.ensure_default_jobs_all_users(conn, s)
    assert len(await job_repo.pending_of_kind(conn, s.USER_ID, "morning_brief")) == 1
    assert len(await job_repo.pending_of_kind(conn, other["id"], "morning_brief")) == 1
    # each user's brief is at 08:00 in *their* timezone
    b_berlin = (await job_repo.pending_of_kind(conn, s.USER_ID, "morning_brief"))[0]["run_at"]
    b_ny = (await job_repo.pending_of_kind(conn, other["id"], "morning_brief"))[0]["run_at"]
    assert (b_ny - b_berlin).total_seconds() % 86400 == 6 * 3600
    # cross-user claiming
    from core import queue
    await observation_repo.insert(conn, Observation(user_id=other["id"], source="gmail", source_key="o1", kind="message_in", occurred_at=datetime.now(tz=UTC), payload={}))
    claimed = await queue.claim_next(conn, None)
    assert claimed is not None and claimed.user_id == other["id"]
    assert TriageResult(urgency=1, category="other", reason="x").summary == ""


async def test_start_command_is_first_contact_not_help(conn, settings):
    from workers.commands import START_TEXT, as_chat_text, is_remind
    assert as_chat_text("/start") == START_TEXT and as_chat_text("/start@proactiveagent_bot") == START_TEXT
    assert as_chat_text("/help") == "help" and as_chat_text("merhaba") == "merhaba"
    assert is_remind("/remind 1h x") and not is_remind("/start")

    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "888", display_name="Ece")
    tg = FakeTelegram()
    calls = []
    def handle(payload):
        calls.append(payload)
        assert payload["task"] == "chat" and payload["message"] == START_TEXT and payload["user_state"]["is_new"]
        return {"task": "chat", "reply": "Selam Ece, kurulumu hemen yapalım.", "intents": []}
    start = await observation_repo.insert(conn, tg_text("888", "1", "/start", user["id"]))
    await triage.handle(start, settings=settings, agent=AgentClient("local", handle_fn=handle), notifier=triage.Notifier(tg, "888", user["id"]),
                        embedder=FakeEmbedder())
    assert len(calls) == 1 and tg.sent[-1]["text"] == "Selam Ece, kurulumu hemen yapalım."


async def test_account_deletion_two_confirmations(conn, settings):
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "444", display_name="Can", language="tr")
    await observation_repo.insert(conn, Observation(user_id=user["id"], source="gmail", source_key="g1", kind="message_in",
                                                    occurred_at=datetime.now(tz=UTC), payload={"from": "X <x@y.test>", "subject": "s"}))
    comp = FakeComposio(); comp.accounts[str(user["id"])] = {"gmail": "ca_1"}
    calls = []
    async def disconnect_all(handle):
        calls.append(handle.data["composio_user_id"]); return {"triggers": 1, "accounts": 1}
    comp.disconnect_all = disconnect_all
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": comp, "telegram": tg})
    notifier = triage.Notifier(tg, "444", user["id"])
    ctx = ticks.TickContext(settings=settings, registry=registry, notifier=notifier)

    def handle(payload):
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": ""}
        return {"task": "chat", "reply": "Tamam, onay soracağım.", "intents": [{"intent": "DeleteAccountRequest"}]}
    from workers import chat
    msg = await observation_repo.insert(conn, tg_text("444", "1", "hesabımı sil", user["id"]))
    await chat.handle_message(conn, msg, settings=settings, agent=AgentClient("local", handle_fn=handle), notifier=notifier,
                              embedder=FakeEmbedder(), registry=registry)
    ask1 = tg.sent[-1]
    assert "Emin misin" in ask1["text"] and [c["text"] for c in ask1["markup"]["inline_keyboard"][0]] == ["Evet, hesabımı sil", "Vazgeç"]
    yes1 = ask1["markup"]["inline_keyboard"][0][0]["callback_data"]

    def cb(data, key):
        return Observation(user_id=user["id"], source="telegram", source_key=key, kind="message_in", occurred_at=datetime.now(tz=UTC),
                           thread_key="444", payload={"update_id": int(key), "callback_query": {"id": f"cq{key}", "data": data, "message": {"message_id": 1, "chat": {"id": 444}}}})

    # step 1 -> second question; nothing deleted yet
    await triage.handle(await observation_repo.insert(conn, cb(yes1, "2")), settings=settings, agent=None, notifier=notifier, tick_ctx=ctx)
    ask2 = tg.sent[-1]
    assert "geri alınamaz" in ask2["text"] and calls == [] and await user_repo.get(conn, user["id"]) is not None
    # tapping the first button again does nothing harmful
    await triage.handle(await observation_repo.insert(conn, cb(yes1, "3")), settings=settings, agent=None, notifier=notifier, tick_ctx=ctx)
    assert calls == []
    # cancel path works at step 2
    no = ask2["markup"]["inline_keyboard"][0][1]["callback_data"]
    await triage.handle(await observation_repo.insert(conn, cb(no, "4")), settings=settings, agent=None, notifier=notifier, tick_ctx=ctx)
    assert tg.sent[-1]["text"] == "Tamam, hiçbir şey silinmedi." and (await user_repo.get(conn, user["id"]))["state"].get("pending_deletion") is None

    # full path: request -> yes1 -> yes2 -> composio disconnected, rows purged, farewell sent
    msg2 = await observation_repo.insert(conn, tg_text("444", "5", "verilerimi sil", user["id"]))
    await chat.handle_message(conn, msg2, settings=settings, agent=AgentClient("local", handle_fn=handle), notifier=notifier,
                              embedder=FakeEmbedder(), registry=registry)
    yes1 = tg.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]
    await triage.handle(await observation_repo.insert(conn, cb(yes1, "6")), settings=settings, agent=None, notifier=notifier, tick_ctx=ctx)
    yes2 = tg.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]
    assert yes2.startswith("del:yes2:")
    await triage.handle(await observation_repo.insert(conn, cb(yes2, "7")), settings=settings, agent=None, notifier=notifier, tick_ctx=ctx)
    assert calls == [str(user["id"])]
    assert tg.sent[-1]["text"].startswith("Bitti.")
    assert await user_repo.get(conn, user["id"]) is None
    cur = await conn.execute("select count(*) as n from observation where user_id = %s", (user["id"],))
    assert (await cur.fetchone())["n"] == 0

    # expiry: a stale request is refused
    user2, _ = await user_repo.get_or_create_by_control(conn, "telegram", "445", language="en")
    await user_repo.merge_state(conn, user2["id"], {"pending_deletion": {"step": 1, "started_at": (datetime.now(tz=UTC) - timedelta(minutes=11)).isoformat()}})
    n2 = triage.Notifier(tg, "445", user2["id"])
    out = await account.on_callback(conn, user2["id"], f"del:yes1:{user2['id'].hex}", n2, registry)
    assert out.startswith("The deletion request expired") and await user_repo.get(conn, user2["id"]) is not None
