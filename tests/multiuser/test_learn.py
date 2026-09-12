"""First-learn after a service is connected. No LLM: fake execute, scripted agent."""
from __future__ import annotations

from datetime import UTC, datetime

from adapters.composio.adapter import ComposioAdapter
from adapters.composio.profile import sample_gmail
from agent.client import AgentClient
from agent.learn_agent import render_facts
from core.adapter import AdapterRegistry
from core.embeddings import FakeEmbedder
from core.models import Observation
from core.repo import job_repo, memory_repo, user_repo
from tests.multiuser.test_multiuser import FakeComposio, FakeTelegram, tg_text
from tests.phase1.test_worker_flow import fake_agent  # noqa: F401
from workers import chat, learn, scheduler, ticks, triage


def _inbox_page(n: int, token: str | None):
    msgs = [{"messageId": f"m{n}{i}", "sender": "Mara Lindqvist <mara@client.test>" if i % 2 else "News <news@list.test>",
             "to": "me@x.test", "subject": f"Invoice #{2040 + i}" if i % 2 else "Weekly digest", "messageTimestamp": f"2026-09-0{1 + i % 9}T09:00:00Z"} for i in range(n)]
    return {"successful": True, "data": {"messages": msgs, **({"nextPageToken": token} if token else {})}}


def test_sample_gmail_counts_people_and_subjects():
    calls = []
    def execute(slug, args, composio_user_id):
        calls.append((slug, args.get("query"), args.get("page_token")))
        if "in:sent" in args["query"]:
            return {"successful": True, "data": {"messages": [{"messageId": "s1", "to": "Jonas Weber <jonas@startup.test>", "subject": "Re: contract", "messageTimestamp": "2026-09-05T10:00:00Z"}]}}
        return _inbox_page(6, "p2") if not args.get("page_token") else _inbox_page(4, None)
    facts = sample_gmail(execute, "cu-1", days=30, max_messages=50)
    assert calls[0][0] == "GMAIL_FETCH_EMAILS" and "in:inbox" in calls[0][1] and calls[1][2] == "p2"
    contacts = {(f["direction"], f["email"]): f["count"] for f in facts if f["kind"] == "contact"}
    assert contacts[("in", "mara@client.test")] == 5 and contacts[("in", "news@list.test")] == 5 and contacts[("out", "jonas@startup.test")] == 1
    subjects = [f for f in facts if f["kind"] == "subject"]
    assert len(subjects) == 10 and subjects[0]["from"] in ("News", "Mara Lindqvist")
    text = render_facts("gmail", facts)
    assert "people (direction, count):" in text and "Mara Lindqvist <mara@client.test> in x5" in text and "Invoice #2041" in text


async def test_first_learn_flow_and_confirmation(conn, settings):
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "901", display_name="Deniz", language="tr")
    comp = FakeComposio()
    async def sample_for_profile(handle, toolkit):
        return [{"kind": "contact", "direction": "in", "name": "Mara Lindqvist", "email": "mara@client.test", "count": 9},
                {"kind": "subject", "text": "Invoice #2041", "from": "Mara Lindqvist", "when": "2026-09-01"}]
    comp.sample_for_profile = sample_for_profile
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": comp, "telegram": tg})
    notifier = triage.Notifier(tg, "901", user["id"])
    seen = []
    def handle(payload):
        seen.append(payload["task"])
        if payload["task"] == "learn":
            assert payload["service"] == "gmail" and payload["facts"][0]["email"] == "mara@client.test" and payload["user"]["language"] == "tr"
            return {"task": "learn", "facts": ["Mara Lindqvist ile sık yazışıyor (müşteri, faturalar)", "Faturalar önemli"],
                    "profile_suggestion": "Freelance, müşteri faturaları acil", "top_people": ["Mara Lindqvist"],
                    "message": "Gmail'ine baktım: en çok Mara Lindqvist ile yazışıyorsun, faturalar öne çıkıyor. Doğru mu?"}
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": ""}
        st = payload["user_state"]["pending_learn"]
        assert st["facts"][0].startswith("Mara") and st["toolkit"] == "gmail"
        return {"task": "chat", "reply": "Tamam, not aldım.", "intents": [
            {"intent": "LearnConfirm", "facts": ["Mara Lindqvist iş arkadaşı, faturaları o gönderir"], "profile": "Freelance, faturalar acil"}]}
    agent = AgentClient("local", handle_fn=handle)

    # connection -> first_learn job scheduled
    await learn.schedule(conn, user["id"], "gmail", delay_s=0)
    jobs = await job_repo.pending_of_kind(conn, user["id"], "first_learn")
    assert len(jobs) == 1 and jobs[0]["payload"] == {"toolkit": "gmail"}
    await scheduler.fire_due(conn, user["id"], datetime.now(tz=UTC), settings.TIMEZONE)
    tick = next(o for o in await __import__("core.repo.observation_repo", fromlist=["x"]).list_since(conn, user["id"], datetime(2020, 1, 1, tzinfo=UTC)) if o.kind == "tick")
    ctx = ticks.TickContext(settings=settings, registry=registry, notifier=notifier, agent=agent, embedder=FakeEmbedder())
    await ticks.handle_tick(conn, tick, ctx)
    assert seen == ["learn"] and tg.sent[-1]["text"].startswith("Gmail'ine baktım")
    u = await user_repo.get(conn, user["id"])
    assert u["state"]["pending_learn"]["facts"][0].startswith("Mara") and u["profile"] == ""   # nothing stored yet
    cur = await conn.execute("select value from identity where user_id = %s", (user["id"],))
    assert [r["value"] for r in await cur.fetchall()] == ["mara@client.test"]                   # people registered
    assert await memory_repo.count(conn, user["id"]) == 0

    # the person corrects -> chat agent calls confirm_learned -> memory + profile written, proposal cleared
    reply = await chat.handle_message(conn, await __import__("core.repo.observation_repo", fromlist=["x"]).insert(conn, tg_text("901", "7", "Mara müşteri değil iş arkadaşım, gerisi doğru", user["id"])),
                                      settings=settings, agent=agent, notifier=notifier, embedder=FakeEmbedder(), registry=registry)
    assert reply == "Tamam, not aldım."
    assert await memory_repo.count(conn, user["id"]) == 1
    u = await user_repo.get(conn, user["id"])
    assert u["profile"] == "Freelance, faturalar acil" and u["state"].get("pending_learn") is None
    assert learn.pending_for_state(u) is None


def test_adapter_sample_dispatch(settings):
    import asyncio

    from core.models import Connection
    a = ComposioAdapter(settings=settings, execute=lambda slug, args, *, user_id: _inbox_page(2, None))
    facts = asyncio.run(a.sample_for_profile(Connection(adapter_id="composio", user_id=settings.USER_ID, data={"composio_user_id": "x"}), "gmail"))
    assert any(f["kind"] == "contact" for f in facts)
    assert asyncio.run(a.sample_for_profile(Connection(adapter_id="composio", user_id=settings.USER_ID, data={"composio_user_id": "x"}), "nothing")) == []
    assert Observation(user_id=settings.USER_ID, source="x", source_key="k", kind="tick", occurred_at=datetime.now(tz=UTC)).status == "new"


async def test_pending_learn_skips_fast_reflex(conn, settings):
    from datetime import UTC, datetime

    from agent.client import AgentClient
    from core.embeddings import FakeEmbedder
    from core.repo import memory_repo, observation_repo, user_repo
    from tests.multiuser.test_multiuser import tg_text
    from tests.phase1.test_worker_flow import FakeTelegram
    from workers import chat, triage

    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "901", language="tr")
    await user_repo.merge_state(conn, user["id"], {"pending_learn": {"toolkit": "notion", "facts": ["Tesseract adlı ürün üzerinde çalışıyor"],
                                                                     "profile_suggestion": "Karlsruhe'de öğrenci", "at": datetime.now(tz=UTC).isoformat()}})
    calls = []
    def handle(payload):
        calls.append(payload["task"])
        if payload["task"] == "chat_ack":
            raise AssertionError("fast reflex must be skipped while a learn proposal is pending")
        assert payload["user_state"]["pending_learn"]["facts"] == ["Tesseract adlı ürün üzerinde çalışıyor"]
        return {"task": "chat", "reply": "Kaydettim.", "intents": [{"intent": "LearnConfirm", "facts": ["Tesseract adlı ürün üzerinde çalışıyor"], "profile": "Karlsruhe'de öğrenci"}]}
    tg = FakeTelegram()
    msg = await observation_repo.insert(conn, tg_text("901", "1", "evet doğru", user["id"]))
    await chat.handle_message(conn, msg, settings=settings, agent=AgentClient("local", handle_fn=handle), notifier=triage.Notifier(tg, "901", user["id"]),
                              embedder=FakeEmbedder())
    assert calls == ["chat"] and await memory_repo.count(conn, user["id"]) == 1
    u = await user_repo.get(conn, user["id"])
    assert u["profile"] == "Karlsruhe'de öğrenci" and u["state"].get("pending_learn") is None


def test_learn_prompt_does_not_trust_prior_profile():
    from agent.learn_agent import LEARN_SYSTEM_PROMPT
    assert "treat it as a hint" in LEARN_SYSTEM_PROMPT and "traceable to the sampled metadata" in LEARN_SYSTEM_PROMPT


async def test_bootstrap_ignores_synthetic_profile(conn, settings):
    from uuid import uuid4

    from core.repo import user_repo
    row = await user_repo.ensure_bootstrap(conn, user_id=uuid4(), control_source="telegram", control_thread_key="4242",
                                           composio_user_id="x", profile="Synthetic profile: developer in Berlin", language="tr", timezone="UTC")
    assert row["profile"] == ""
