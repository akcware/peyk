from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent import app as agent_app
from agent.chat_agent import make_tools, to_messages
from agent.client import AgentClient
from core.embeddings import FakeEmbedder
from core.models import Observation
from core.repo import job_repo, memory_repo, observation_repo
from tests.conftest import USER_ID
from tests.phase1.test_worker_flow import FakeTelegram, gmail_obs
from workers import chat, triage

ROOT = Path(__file__).resolve().parents[2]


def tg_text(key: str, text: str, when: datetime) -> Observation:
    return Observation(user_id=USER_ID, source="telegram", source_key=key, kind="message_in", occurred_at=when,
                       thread_key="777", payload={"update_id": int(key), "message": {"text": text, "chat": {"id": 777}}})


def tg_out(key: str, text: str, when: datetime) -> Observation:
    return Observation(user_id=USER_ID, source="telegram", source_key=f"out:h{key}", kind="message_out", occurred_at=when,
                       thread_key="777", payload={"text": text})


def scripted_agent(script):
    """script: list of {"reply", "intents"} consumed per call; records payloads."""
    calls: list[dict] = []

    def handle(payload):
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": ""}   # silent ack in most tests
        calls.append(payload)
        step = script[min(len(calls) - 1, len(script) - 1)]
        return {"task": "chat", "reply": step["reply"], "intents": step.get("intents", [])}
    return AgentClient("local", handle_fn=handle), calls


async def test_chat_history_window(conn, settings):
    t0 = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
    for i in range(15):
        await observation_repo.insert(conn, tg_text(str(100 + i), f"user turn {i}", t0 + timedelta(minutes=2 * i)))
        await observation_repo.insert(conn, tg_out(str(100 + i), f"bot turn {i}", t0 + timedelta(minutes=2 * i, seconds=30)))
    await observation_repo.insert(conn, tg_text("200", "/remind 1h ignored command", t0 + timedelta(minutes=40)))
    current = await observation_repo.insert(conn, tg_text("201", "what did we talk about?", t0 + timedelta(minutes=41)))

    history = await chat.build_history(conn, current)
    assert len(history) == 10
    assert history[0] == {"role": "user", "text": "user turn 10"}
    assert history[-1] == {"role": "assistant", "text": "bot turn 14"}
    assert all("/remind" not in h["text"] for h in history)

    agent, calls = scripted_agent([{"reply": "we talked"}])
    tg = FakeTelegram()
    await chat.handle_message(conn, current, settings=settings, agent=agent, notifier=triage.Notifier(tg, "777", USER_ID),
                              embedder=FakeEmbedder())
    assert len(calls[0]["history"]) == 10 and calls[0]["message"] == "what did we talk about?"
    assert tg.sent[-1]["text"] == "we talked"
    outs = [o for o in await observation_repo.list_by_thread(conn, USER_ID, "777", limit=100) if o.kind == "message_out"]
    assert any(o.payload["text"] == "we talked" and o.payload["in_reply_to"] == str(current.id) for o in outs)


async def test_intents_applied(conn, settings):
    now = datetime.now(tz=UTC)
    msg = await observation_repo.insert(conn, tg_text("300", "remember that I use Postgres; remind me tomorrow", now))
    agent, _ = scripted_agent([{"reply": "ok", "intents": [
        {"intent": "MemoryWrite", "text": "Uses Postgres in the proactive-agent project"},
        {"intent": "ScheduleRequest", "when_iso": "2026-09-11T09:00:00+02:00", "note": "check Postgres"},
        {"intent": "ActionDraft", "channel": "gmail", "body": "hi"},   # ignored until phase 4
        {"intent": "Bogus"},
    ]}])
    tg = FakeTelegram()
    await chat.handle_message(conn, msg, settings=settings, agent=agent, notifier=triage.Notifier(tg, "777", USER_ID), embedder=FakeEmbedder())
    assert await memory_repo.count(conn, USER_ID) == 1
    jobs = await job_repo.pending_of_kind(conn, USER_ID, "followup")
    assert len(jobs) == 1 and jobs[0]["created_by"] == "agent" and jobs[0]["payload"] == {"note": "check Postgres"}
    assert jobs[0]["run_at"] == datetime(2026, 9, 11, 7, 0, tzinfo=UTC)
    cur = await conn.execute("select text, source_observation_id from memory")
    row = await cur.fetchone()
    assert row["text"].startswith("Uses Postgres") and row["source_observation_id"] == msg.id


async def test_need_more_bounded(conn, settings):
    now = datetime.now(tz=UTC)
    old = await observation_repo.insert(conn, gmail_obs("old-contract"))
    await conn.execute("update observation set occurred_at = %s, payload = payload || '{\"subject\": \"contract draft v2\"}' where id = %s",
                       (now - timedelta(days=5), old.id))
    fresh = await observation_repo.insert(conn, gmail_obs("fresh"))
    msg = await observation_repo.insert(conn, tg_text("400", "what about the contract?", now))
    agent, calls = scripted_agent([
        {"reply": "need data", "intents": [{"intent": "NeedMore", "query": "contract", "since_days": 7}]},
        {"reply": "still need", "intents": [{"intent": "NeedMore", "query": "contract", "since_days": 30}]},
        {"reply": "never reached"},
    ])
    tg = FakeTelegram()
    reply = await chat.handle_message(conn, msg, settings=settings, agent=agent, notifier=triage.Notifier(tg, "777", USER_ID), embedder=FakeEmbedder())
    assert len(calls) == 2                                     # bounded
    assert reply == "still need" and tg.sent[-1]["text"] == "still need"
    first_ids = {o["id"] for o in calls[0]["recent_observations"]}
    second_ids = {o["id"] for o in calls[1]["recent_observations"]}
    assert str(fresh.id) in first_ids and str(old.id) not in first_ids     # 48h window first
    assert str(old.id) in second_ids and str(fresh.id) in second_ids        # NeedMore pulled the older one in
    assert calls[1]["recent_observations"][0]["subject"] == "contract draft v2"
    outs = [o for o in await observation_repo.list_by_thread(conn, USER_ID, "777", limit=10) if o.kind == "message_out"]
    assert outs[0].payload["intents"] == []                                  # NeedMore is not persisted as an intent


async def test_memory_search_fake_embedder(conn):
    emb = FakeEmbedder()
    facts = ["Uses Postgres in the proactive-agent project", "Lives in Berlin", "Prefers morning meetings",
             "Client Mara pays invoices late", "Daughter goes to Kita Sonnenschein"]
    for f in facts:
        await memory_repo.insert(conn, USER_ID, f, await emb.embed(f))
    hits = await memory_repo.search(conn, USER_ID, await emb.embed("which project uses Postgres"), k=1)
    assert hits[0]["text"].startswith("Uses Postgres")


def test_chat_contract_and_tools():
    out = agent_app.handle({"task": "chat", "message": "hi", "history": []}, chat_fn=lambda p: {"reply": "hello", "intents": []})
    assert out == {"task": "chat", "reply": "hello", "intents": []}

    intents: list = []
    ctx = {"recent_observations": [
        {"id": "1", "source": "gmail", "from": "Mara <mara@x.test>", "subject": "Invoice 2041", "occurred_at": "2026-09-10T09:00:00+00:00"},
        {"id": "2", "source": "calendar", "summary": "Standup", "occurred_at": "2026-09-10T07:00:00+00:00"},
    ], "memory_hits": [{"text": "Uses Postgres", "score": 0.9}, {"text": "Lives in Berlin", "score": 0.5}]}
    tools = {t.tool_name: t for t in make_tools(ctx, intents)}
    assert set(tools) == {"search_observations", "search_memory", "remember", "schedule_followup", "draft_reply", "find_contact", "connect_service", "set_profile", "need_more"}
    fn = {name: t._tool_func for name, t in tools.items()}
    assert [o["id"] for o in fn["search_observations"]("invoice mara")] == ["1"]
    assert [o["id"] for o in fn["search_observations"]("", "calendar")] == ["2"]
    assert fn["search_observations"]("", "", "2026-09-10T08:00:00+00:00")[0]["id"] == "1"
    assert fn["search_memory"]("postgres")[0]["text"] == "Uses Postgres"
    fn["remember"]("Likes tea"); fn["schedule_followup"]("2026-09-11T09:00:00+02:00", "x")
    fn["draft_reply"]("Hello", "th1", "a@x.test, b@x.test", "Re: hi"); fn["need_more"]("contract", 14)
    assert [i["intent"] for i in intents] == ["MemoryWrite", "ScheduleRequest", "ActionDraft", "NeedMore"]
    assert intents[2]["to"] == ["a@x.test", "b@x.test"] and intents[2]["thread_key"] == "th1"
    assert intents[3]["since_days"] == 14

    msgs = to_messages([{"role": "assistant", "text": "dropped leading"}, {"role": "user", "text": "a"}, {"role": "user", "text": "b"},
                        {"role": "assistant", "text": "c"}, {"role": "assistant", "text": ""}])
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[0]["content"][0]["text"] == "a\nb"


def test_agent_never_touches_db():
    r = subprocess.run(["grep", "-rnE", r"^(from|import) (psycopg|core\.db|core\.repo|core\.embeddings)", "agent", "--include=*.py"],
                       cwd=ROOT, capture_output=True, text=True, check=False)
    assert r.stdout.strip() == "", r.stdout


async def test_history_excludes_later_messages_and_stale_is_skipped(conn, settings):
    t0 = datetime.now(tz=UTC) - timedelta(minutes=1)
    old = await observation_repo.insert(conn, tg_text("500", "merhaba", t0))
    await observation_repo.insert(conn, tg_text("501", "5+5?", t0 + timedelta(seconds=10)))    # arrived later
    history = await chat.build_history(conn, old)
    assert all(h["text"] != "5+5?" for h in history)

    # stale: received 20 minutes ago -> no reply, nothing sent
    stale = await observation_repo.insert(conn, tg_text("502", "hey", t0))
    await conn.execute("update observation set received_at = now() - interval '20 minutes' where id = %s", (stale.id,))
    stale = await observation_repo.get(conn, stale.id)
    agent, calls = scripted_agent([{"reply": "late"}])
    tg = FakeTelegram()
    out = await chat.handle_message(conn, stale, settings=settings, agent=agent, notifier=triage.Notifier(tg, "777", USER_ID), embedder=FakeEmbedder())
    assert out == "" and calls == [] and tg.sent == []

    # idempotent: a message that already has a reply is not answered twice (retry after a late failure)
    fresh = await observation_repo.insert(conn, tg_text("503", "hi", datetime.now(tz=UTC)))
    await chat.handle_message(conn, fresh, settings=settings, agent=agent, notifier=triage.Notifier(tg, "777", USER_ID), embedder=FakeEmbedder())
    await chat.handle_message(conn, fresh, settings=settings, agent=agent, notifier=triage.Notifier(tg, "777", USER_ID), embedder=FakeEmbedder())
    assert len(tg.sent) == 1 and len(calls) == 1


def test_render_unescapes_html_and_plain_reason():
    obs = Observation(user_id=USER_ID, source="gmail", source_key="h", kind="message_in", occurred_at=datetime.now(tz=UTC),
                      payload={"from": "G <g@x.test>", "subject": "Security alert", "snippet": "you didn&#39;t allow &amp; more"})
    from agent.schemas import TriageResult
    text = triage.Notifier.render(obs, TriageResult(urgency=4, category="automated", reason="verify"))
    assert text.startswith("❗ G · gmail\nSecurity alert") and "didn't allow & more" in text and "_verify_" not in text
    with_summary = triage.Notifier.render(obs, TriageResult(urgency=4, category="automated", reason="verify",
                                                             summary="Google says Composio got access; confirm it was you."))
    assert "confirm it was you" in with_summary and "didn't allow" not in with_summary


async def test_secretary_ack_then_answer_and_direct_answer(conn, settings):
    from workers.triage import md_to_telegram_html

    assert md_to_telegram_html("**Google** — \"Alert\" & `x` _y_") == '<b>Google</b> — "Alert" &amp; <code>x</code> <i>y</i>'
    assert md_to_telegram_html("a < b") == "a &lt; b"

    def handle(payload):
        if payload["task"] == "chat_ack":
            if "mail" in payload["message"]:
                return {"task": "chat_ack", "needs_work": True, "message": "Tabii, bugünkü maillere hemen bakıyorum."}
            return {"task": "chat_ack", "needs_work": False, "message": "10!"}
        return {"task": "chat", "reply": "**Google** ve Devpost yazmış.", "intents": []}

    tg = FakeTelegram()
    tg.actions = []
    async def send_chat_action(chat_id, action="typing"):
        tg.actions.append(action)
    tg.send_chat_action = send_chat_action
    notifier = triage.Notifier(tg, "777", USER_ID)
    agent = AgentClient("local", handle_fn=handle)

    q = await observation_repo.insert(conn, tg_text("600", "bugün kim mail attı?", datetime.now(tz=UTC)))
    await chat.handle_message(conn, q, settings=settings, agent=agent, notifier=notifier, embedder=FakeEmbedder())
    assert [m["text"] for m in tg.sent] == ["Tabii, bugünkü maillere hemen bakıyorum.", "<b>Google</b> ve Devpost yazmış."]
    assert tg.actions.count("typing") >= 2
    outs = [o for o in await observation_repo.list_by_thread(conn, USER_ID, "777", limit=10) if o.kind == "message_out"]
    assert {o.payload["stage"] for o in outs if o.payload.get("in_reply_to") == str(q.id) and "stage" in o.payload} == {"ack"}
    # idempotency looks at the final answer, not the ack
    await chat.handle_message(conn, q, settings=settings, agent=agent, notifier=notifier, embedder=FakeEmbedder())
    assert len(tg.sent) == 2

    tg.sent.clear()
    simple = await observation_repo.insert(conn, tg_text("601", "5+5?", datetime.now(tz=UTC)))
    await chat.handle_message(conn, simple, settings=settings, agent=agent, notifier=notifier, embedder=FakeEmbedder())
    assert [m["text"] for m in tg.sent] == ["10!"]          # answered directly, no second stage


async def test_find_contact_round(conn, settings):
    """Agent asks for a contact -> worker looks it up (identity table + adapters) -> agent re-run with results."""
    from core.repo import identity_repo
    from workers import contacts

    await identity_repo.resolve(conn, USER_ID, "email", "Deniz Ateş <deniz.ates@example.test>", display_name="Deniz Ateş")

    class FakeGmailContacts:
        id = "composio"
        def capabilities(self): return {"can_send": True, "needs_session": False, "needs_user_device": False}
        async def connect(self, user_id):
            from core.models import Connection
            return Connection(adapter_id=self.id, user_id=user_id)
        async def search_contacts(self, conn, query):
            return [{"name": "Deniz Ateş", "email": "deniz@work.test", "source": "google_contacts"}]

    from core.adapter import AdapterRegistry
    search = contacts.make_contact_search(AdapterRegistry({"composio": FakeGmailContacts()}), USER_ID)
    calls: list[dict] = []

    def handle(payload):
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": ""}
        calls.append(payload)
        if not payload.get("contacts"):
            return {"task": "chat", "reply": "looking up", "intents": [{"intent": "FindContact", "name": "Deniz Ateş"}]}
        emails = sorted(c.get("email") for c in payload["contacts"] if c.get("email"))
        return {"task": "chat", "reply": f"draft ready for {emails[0]}", "intents": [
            {"intent": "ActionDraft", "channel": "gmail", "to": [emails[0]], "subject": "Fwd", "body": "hi"}]}

    tg = FakeTelegram()
    msg = await observation_repo.insert(conn, tg_text("700", "bu maili deniz ateşe gönderir misin?", datetime.now(tz=UTC)))
    reply = await chat.handle_message(conn, msg, settings=settings, agent=AgentClient("local", handle_fn=handle),
                                      notifier=triage.Notifier(tg, "777", USER_ID), embedder=FakeEmbedder(), contact_search=search)
    assert len(calls) == 2
    found = calls[1]["contacts"]
    assert {c.get("email") for c in found} == {"deniz.ates@example.test", "deniz@work.test"}
    assert {c["source"] for c in found} == {"known_person", "google_contacts"}
    assert reply.startswith("draft ready for deniz")
    outs = [o for o in await observation_repo.list_by_thread(conn, USER_ID, "777", limit=10) if o.kind == "message_out"]
    assert outs[0].payload["intents"] == ["ActionDraft"]      # FindContact never persisted as an intent

    # tool side: with contacts already in the payload the tool answers without a new intent
    intents: list = []
    tools = {t.tool_name: t for t in make_tools({"contacts": found}, intents)}
    hits = tools["find_contact"]._tool_func("deniz")["contacts"]
    assert len(hits) == 2 and intents == []
    assert tools["find_contact"]._tool_func("nobody here")["contacts"] == found   # falls back to everything known


def test_prompts_state_real_capabilities_only():
    from agent.chat_agent import ACK_SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT, render_capabilities

    caps = render_capabilities({"user_state": {"available": ["gmail", "googlecalendar"], "connected": ["gmail"], "pending": ["googlecalendar"]}})
    assert "Gmail, Google Calendar" in caps and "Connected right now: Gmail" in caps and "in progress: Google Calendar" in caps
    assert "no Outlook, Slack" in caps
    assert "{capabilities}" in ACK_SYSTEM_PROMPT and "{capabilities}" in CHAT_SYSTEM_PROMPT
