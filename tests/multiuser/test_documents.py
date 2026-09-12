"""Document services (Notion / Drive / Docs): search/read/create through fake execute, approval routing,
chat round loop, samplers. No LLM."""
from __future__ import annotations

from datetime import UTC, datetime

from adapters.composio.adapter import ComposioAdapter
from adapters.composio.profile import PROFILE_SAMPLERS
from adapters.composio.setup import TOOLKITS, normalize_toolkit
from agent.chat_agent import make_tools
from agent.client import AgentClient
from core.adapter import CHANNEL_ADAPTERS, DOCUMENT_CHANNELS, AdapterRegistry
from core.embeddings import FakeEmbedder
from core.models import Connection, Content
from core.repo import action_repo, observation_repo, user_repo
from tests.conftest import USER_ID
from tests.multiuser.test_multiuser import tg_text
from tests.phase1.test_worker_flow import FakeTelegram
from workers import actions, approval, chat, triage

NOTION_PAGE = {"id": "p1", "url": "https://notion.so/p1", "last_edited_time": "2026-09-10T10:00:00Z",
               "properties": {"Name": {"type": "title", "title": [{"plain_text": "Apartment hunt"}]}}}
DOC = {"id": "d1", "name": "Hackathon plan", "modifiedTime": "2026-09-11T08:00:00Z", "webViewLink": "https://docs.google.com/document/d/d1"}


def fake_execute(calls):
    def execute(slug, args, *, user_id=None):
        calls.append((slug, args, user_id))
        if slug == "NOTION_SEARCH_NOTION_PAGE":
            return {"successful": True, "data": {"results": [NOTION_PAGE]}}
        if slug == "NOTION_GET_PAGE_MARKDOWN":
            return {"successful": True, "data": {"markdown": "# Apartment hunt\n- call landlord"}}
        if slug == "NOTION_FETCH_ROW":
            return {"successful": True, "data": {"url": "https://notion.so/p1", "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Apartment hunt"}]},
                "Datum": {"type": "date", "date": {"start": "2026-08-26", "end": "2026-09-24"}},
                "Status": {"type": "status", "status": {"name": "Not started"}},
                "Ort": {"type": "rich_text", "rich_text": []}}}}
        if slug == "NOTION_CREATE_NOTION_PAGE":
            return {"successful": True, "data": {"id": "p_new", "url": "https://notion.so/p_new"}}
        if slug == "NOTION_FETCH_DATA":
            return {"successful": True, "data": {"results": [{"id": "db1", "title": [{"plain_text": "Tasks"}]}]}}
        if slug == "GOOGLEDRIVE_LIST_FILES":
            return {"successful": True, "data": {"files": [{**DOC, "mimeType": "application/vnd.google-apps.document"}]}}
        if slug == "GOOGLEDOCS_SEARCH_DOCUMENTS":
            return {"successful": True, "data": {"documents": [DOC]}}
        if slug == "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT":
            return {"successful": True, "data": {"text": "Plan: ship by Monday"}}
        if slug == "GOOGLEDOCS_CREATE_DOCUMENT_MARKDOWN":
            return {"successful": True, "data": {"documentId": "d_new"}}
        raise AssertionError(f"unexpected slug {slug}")
    return execute


class DocsAdapter(ComposioAdapter):
    """Real adapter code with fake execute and fake connected accounts."""

    def __init__(self, settings, calls, connected):
        super().__init__(settings=settings, execute=fake_execute(calls))
        self._connected = connected

    async def connected_toolkits(self, conn):
        return dict(self._connected)


def test_toolkits_and_aliases():
    for slug in ("notion", "googledrive", "googledocs"):
        assert slug in TOOLKITS and TOOLKITS[slug]["auth_config_env"].startswith("COMPOSIO_") and slug in PROFILE_SAMPLERS
    assert normalize_toolkit("drive") == "googledrive" and normalize_toolkit("Google Docs") == "googledocs" and normalize_toolkit("notion") == "notion"
    assert CHANNEL_ADAPTERS["notion"] == "composio" and CHANNEL_ADAPTERS["googledocs"] == "composio"
    assert DOCUMENT_CHANNELS == {"notion": "Notion page", "googledocs": "Google Doc"}


async def test_search_read_create(settings):
    calls = []
    a = DocsAdapter(settings, calls, {"notion": "ca_n", "googledocs": "ca_d", "googledrive": "ca_g"})
    conn = Connection(adapter_id="composio", user_id=USER_ID, data={"composio_user_id": "u-1"})
    res = await a.search_documents(conn, "plan")
    assert {(r["service"], r["id"], r["title"]) for r in res} == {("notion", "p1", "Apartment hunt"), ("googledrive", "d1", "Hackathon plan"), ("googledocs", "d1", "Hackathon plan")}
    assert all(c[2] == "u-1" for c in calls)                       # per-user entity id
    only = await a.search_documents(conn, "plan", ["notion"])
    assert [r["service"] for r in only] == ["notion"]
    doc = await a.read_document(conn, "notion", "p1")
    assert doc["title"] == "Apartment hunt" and "Datum: 2026-08-26 → 2026-09-24" in doc["text"] and "Status: Not started" in doc["text"]
    assert "Ort:" not in doc["text"] and doc["text"].endswith("- call landlord")
    gd = await a.read_document(conn, "googledocs", "d1")
    assert gd["text"] == "Plan: ship by Monday" and gd["url"].endswith("/d1/edit")
    created = await a.create_document(conn, "notion", "Notes", "- a\n- b", parent="p1")
    assert created == {"id": "p_new", "url": "https://notion.so/p_new"}
    assert next(c for c in calls if c[0] == "NOTION_CREATE_NOTION_PAGE")[1] == {"parent_id": "p1", "title": "Notes", "markdown": "- a\n- b"}
    # no parent -> most recent page becomes the parent
    calls.clear()
    await a.create_document(conn, "notion", "Notes", "x", parent=None)
    assert [c[0] for c in calls] == ["NOTION_SEARCH_NOTION_PAGE", "NOTION_CREATE_NOTION_PAGE"]
    gdoc = await a.create_document(conn, "googledocs", "Plan", "# Plan")
    assert gdoc["id"] == "d_new" and gdoc["url"].endswith("/d_new/edit")
    # "send" on a document channel creates the document
    ext = await a.send(conn, "", Content(text="body", subject="Title", extra={"channel": "googledocs"}))
    assert ext == "d_new"
    # samplers
    for svc in ("notion", "googledrive", "googledocs"):
        facts = PROFILE_SAMPLERS[svc](a._execute, "u-1")
        assert facts and all(f["kind"] in ("document", "database") for f in facts) and facts[0]["service"] == svc


async def test_document_draft_through_approval(conn, settings):
    calls = []
    gmail_like = DocsAdapter(settings, calls, {"notion": "ca_n"})
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": gmail_like, "telegram": tg})
    notifier = triage.Notifier(tg, "777", USER_ID)
    flow = approval.ApprovalFlow(registry, notifier)
    src = await observation_repo.insert(conn, tg_text("777", "1", "write me a notion page with the plan", USER_ID))
    action = await flow.on_draft(conn, src, {"intent": "ActionDraft", "channel": "notion", "subject": "Plan", "body": "- ship", "thread_key": None, "to": []})
    draft = tg.sent[-1]
    assert draft["text"].startswith("📝 Draft — Notion page\nTitle: Plan") and draft["markup"]["inline_keyboard"][0][0]["text"] == "✅ Create"
    approved = await actions.approve(conn, action["id"], action["content_hash"])
    sent = await actions.send(conn, approved["id"], gmail_like, await gmail_like.connect(USER_ID))
    assert sent["status"] == "sent" and sent["external_id"] == "p_new"
    assert (await action_repo.get(conn, action["id"]))["channel"] == "notion"


async def test_chat_round_resolves_document_query(conn, settings):
    calls = []
    a = DocsAdapter(settings, calls, {"googledocs": "ca_d"})
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": a, "telegram": tg})
    seen = []

    def handle(payload):
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": ""}
        seen.append(payload["documents"])
        if not payload["documents"]["search"]:
            return {"task": "chat", "reply": "looking", "intents": [{"intent": "DocumentQuery", "op": "search", "query": "plan", "service": None, "key": "*|plan"}]}
        hits = payload["documents"]["search"]["*|plan"]
        return {"task": "chat", "reply": f"found {hits[0]['title']}", "intents": []}
    await user_repo.get_or_create_by_control(conn, "telegram", "777")
    msg = await observation_repo.insert(conn, tg_text("777", "2", "find my plan doc", USER_ID))
    reply = await chat.handle_message(conn, msg, settings=settings, agent=AgentClient("local", handle_fn=handle), notifier=triage.Notifier(tg, "777", USER_ID),
                                      embedder=FakeEmbedder(), registry=registry)
    assert reply == "found Hackathon plan" and len(seen) == 2
    assert [c[0] for c in calls] == ["GOOGLEDOCS_SEARCH_DOCUMENTS"]   # only the connected service was searched
    # tool side: cached results answer directly; missing -> intent
    intents = []
    tools = {t.tool_name: t for t in make_tools({"documents": {"search": {"*|plan": [DOC]}, "read": {}}}, intents)}
    assert tools["search_documents"]._tool_func("plan")["results"] == [DOC] and intents == []
    tools["read_document"]._tool_func("googledocs", "d1")
    assert intents[-1]["intent"] == "DocumentQuery" and intents[-1]["op"] == "read"
    tools["create_document"]._tool_func("googledocs", "T", "# body")
    assert intents[-1] == {"intent": "DocumentCreate", "service": "googledocs", "title": "T", "body": "# body", "parent": None}
    assert datetime.now(tz=UTC).year >= 2026


async def test_document_create_is_direct_and_links(conn, settings):
    """create_document creates immediately (own workspace) and the link follows the reply; failures do not retry."""
    calls = []
    a = DocsAdapter(settings, calls, {"googledocs": "ca_d"})
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": a, "telegram": tg})
    def handle(payload):
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": "Hazırlıyorum."}
        return {"task": "chat", "reply": "Kira sözleşmesi taslağını oluşturdum.", "intents": [
            {"intent": "DocumentCreate", "service": "googledocs", "title": "Kira Sözleşmesi", "body": "# Kira", "parent": None}]}
    await user_repo.get_or_create_by_control(conn, "telegram", "777")
    msg = await observation_repo.insert(conn, tg_text("777", "3", "örnek kira sözleşmesi hazırla", USER_ID))
    await chat.handle_message(conn, msg, settings=settings, agent=AgentClient("local", handle_fn=handle), notifier=triage.Notifier(tg, "777", USER_ID),
                              embedder=FakeEmbedder(), registry=registry)
    texts = [m["text"] for m in tg.sent]
    assert texts[0] == "Hazırlıyorum." and texts[1].startswith("Kira sözleşmesi taslağını")
    assert texts[2] == "📄 Kira Sözleşmesi\nhttps://docs.google.com/document/d/d_new/edit"
    assert [c[0] for c in calls] == ["GOOGLEDOCS_CREATE_DOCUMENT_MARKDOWN"]

    # a model failure in stage 2: apology, no exception (so the queue does not retry), and a retry would not re-ack
    tg.sent.clear()
    def boom(payload):
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": "Bakıyorum."}
        raise RuntimeError("max tokens")
    msg2 = await observation_repo.insert(conn, tg_text("777", "4", "uzun bir şey yaz", USER_ID))
    await chat.handle_message(conn, msg2, settings=settings, agent=AgentClient("local", handle_fn=boom), notifier=triage.Notifier(tg, "777", USER_ID),
                              embedder=FakeEmbedder(), registry=registry)
    assert [m["text"] for m in tg.sent] == ["Bakıyorum.", "I got stuck on that one — could you ask again?"]
