"""Calendar changes as news: the EVENT_SYNC mapping, the review (own edits, replays, invites, moves, cancellations,
guests' answers), "already told" against Google's own calendar mails in both orders, and the one-time calendar
setup (idempotent triggers, owner address, event baseline)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from adapters.composio import calendar as gcal
from adapters.composio.adapter import ComposioAdapter
from adapters.composio.setup import TOOLKITS
from adapters.composio.webhook import to_observation
from agent.client import AgentClient
from agent.schemas import TriageResult
from core.adapter import AdapterRegistry
from core.models import Observation
from core.repo import budget_repo, observation_repo, user_repo
from tests.phase1.test_worker_flow import FakeTelegram
from workers import calendar_sync, chat, pretriage, triage

SYNC = "GOOGLECALENDAR_GOOGLE_CALENDAR_EVENT_SYNC_TRIGGER"
STARTING = "GOOGLECALENDAR_EVENT_STARTING_SOON_TRIGGER"
ME, EKIN = "me@example.test", "ekin@example.test"
NOW = datetime.now(tz=UTC).replace(microsecond=0)


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def change(**over) -> dict:
    """An EVENT_SYNC payload (keys from triggers.get_type, 2026-09-14): Ekin's meeting with the person, just updated."""
    base = {"event_id": "ev1", "event_type": "updated", "summary": "Kickoff", "status": "confirmed",
            "start_time": "2026-09-15T12:00:00+02:00", "end_time": "2026-09-15T13:00:00+02:00",
            "organizer_email": EKIN, "organizer_name": "Ekin Derin", "creator_email": EKIN, "calendar_id": "primary",
            "attendees": [{"email": ME, "self": True, "responseStatus": "needsAction"},
                          {"email": EKIN, "displayName": "Ekin Derin", "responseStatus": "accepted", "organizer": True}],
            "created_at": ago(24), "updated_at": NOW.isoformat()}
    return {**base, **over}


def obs_of(user_id, payload: dict) -> Observation:
    return to_observation({"trigger_slug": SYNC, "payload": payload, "metadata": {}}, user_id)


def review(cur: dict, prev: dict | None = None, own=(ME,)):
    return gcal.review_change(obs_of(uuid4(), cur).payload, prev, set(own), NOW)


def test_sync_trigger_and_mapping():
    assert SYNC in TOOLKITS["googlecalendar"]["triggers"] and STARTING in TOOLKITS["googlecalendar"]["triggers"]
    o = obs_of(uuid4(), change())
    assert o.source == "calendar" and o.kind == "event_changed" and o.thread_key == "ev1"
    assert o.source_key == f"ev1:{NOW.isoformat()}" and o.occurred_at == NOW
    assert o.payload["change_type"] == "updated" and o.payload["organizer_email"] == EKIN and len(o.payload["attendees"]) == 2


def test_review_decides_what_is_news():
    prev = obs_of(uuid4(), change(updated_at=ago(2))).payload
    # someone invites the person: news, from the organizer
    inv = review(change(event_type="created"))
    assert inv == {"event_title": "kickoff", "change": "invited", "from": "Ekin Derin <ekin@example.test>"}
    # the person's own new event (or one Peyk made for them): not news, even before their address is known
    assert review(change(event_type="created", organizer_email=ME, attendees=[])) is None
    assert review(change(event_type="created", organizer_email=ME), own=()) is None        # Google marks their entry "self"
    # the organizer moves it; the same instant written in another zone is no move
    moved = review(change(start_time="2026-09-15T15:00:00+02:00", end_time="2026-09-15T16:00:00+02:00"), prev)
    assert moved["change"] == "moved" and moved["was_start"] == "2026-09-15T12:00:00+02:00" and moved["from"].startswith("Ekin Derin")
    assert review(change(start_time="2026-09-15T10:00:00+00:00", end_time="2026-09-15T11:00:00+00:00"), prev) is None
    # the person answers the invitation themselves: not news
    mine_accepted = [{"email": ME, "self": True, "responseStatus": "accepted"}, {"email": EKIN, "responseStatus": "accepted"}]
    assert review(change(attendees=mine_accepted), prev) is None
    # a new place is worth telling, a new description is not
    assert review(change(location="Room B"), prev) == {"event_title": "kickoff", "change": "changed", "from": "Ekin Derin <ekin@example.test>",
                                                       "was_location": ""}
    assert review(change(description="agenda attached"), prev) is None
    # an update to an event never seen before: nothing to compare with
    assert review(change()) is None
    # cancelled by the organizer; the deletion carries little, the previous state fills in title and time
    gone = review({"event_id": "ev1", "event_type": "deleted", "status": "cancelled", "updated_at": NOW.isoformat()}, prev)
    assert gone["change"] == "cancelled" and gone["summary"] == "Kickoff" and gone["start_time"] == "2026-09-15T12:00:00+02:00"
    assert gone["from"] == "Ekin Derin <ekin@example.test>" and gone["event_title"] == "kickoff"
    # the person deleting their own event, or removing an invitation they declined: not news
    mine_prev = obs_of(uuid4(), change(organizer_email=ME, updated_at=ago(2))).payload
    assert review({"event_id": "ev1", "event_type": "deleted", "status": "cancelled", "updated_at": NOW.isoformat()}, mine_prev) is None
    declined = [{"email": ME, "self": True, "responseStatus": "declined"}]
    assert review(change(event_type="deleted", status="cancelled", attendees=declined), prev) is None
    # a guest answers the person's own invitation: news; the person moving their own meeting: not
    own_meeting = change(organizer_email=ME, organizer_name="", attendees=[
        {"email": ME, "self": True, "responseStatus": "accepted"}, {"email": EKIN, "displayName": "Ekin Derin", "responseStatus": "needsAction"}])
    before = obs_of(uuid4(), {**own_meeting, "updated_at": ago(1)}).payload
    answer = {**own_meeting, "attendees": [own_meeting["attendees"][0], {**own_meeting["attendees"][1], "responseStatus": "declined"}]}
    assert review(answer, before) == {"event_title": "kickoff", "change": "guest_answered", "from": "Ekin Derin <ekin@example.test>",
                                      "answer": "declined", "guest": "Ekin Derin"}
    assert review({**own_meeting, "start_time": "2026-09-15T16:00:00+02:00"}, before) is None
    # replays from the first poll and late changes: not news; no organizer anywhere: stay quiet
    assert review(change(event_type="created", updated_at=ago(3))) is None
    assert review(change(organizer_email="", organizer_name="", event_type="created")) is None


def test_google_calendar_mail_titles():
    t = gcal.calendar_mail_title
    assert t("Invitation: Kickoff @ Tue Sep 15, 2026 12pm - 1pm (GMT+2) (me@example.test)") == "kickoff"
    assert t("Güncellenmiş davet: Kickoff  @ 15 Eyl 2026 Sal 12:00 - 13:00 (GMT+3) (me@example.test)") == "kickoff"
    assert t("Accepted: Weekly Sync @ Weekly from 10am to 10:30am on Monday (CEST) (me@example.test)") == "weekly sync"
    assert t("Einladung: Projekt: Phase 2 @ Di. 15. Sept. 2026 12:00 - 13:00 (MESZ) (me@example.test)") == "projekt: phase 2"
    assert t("Re: lunch @ 12?") == "" and t("Invoice 2041") == "" and t("") == ""
    assert gcal.event_title_of({"subject": "Canceled event: Kickoff @ Tue Sep 15, 2026 (me@example.test)"}) == "kickoff"
    assert gcal.event_title_of({"event_title": "  Kickoff "}) == "kickoff" and gcal.event_title_of({"subject": "hello"}) == ""


def triage_agent(urgency: int):
    seen: list[dict] = []

    def handle(payload):
        assert payload["task"] == "triage"
        seen.append(payload["observation"])
        result = TriageResult(urgency=urgency, category="calendar", reason="r", summary="s")
        return {"task": "triage", "result": result.model_dump(), "model_id": "fake", "latency_ms": 1}
    return AgentClient("local", handle_fn=handle), seen


def mail(user_id, key: str, subject: str) -> Observation:
    return Observation(user_id=user_id, source="gmail", source_key=key, kind="message_in", occurred_at=NOW, thread_key=f"th-{key}",
                       payload={"from": "Ekin Derin <ekin@example.test>", "subject": subject, "snippet": "hello"})


async def test_calendar_news_reaches_the_person_once(conn, settings):
    user = await user_repo.create(conn, control_source="telegram", control_thread_key="901", display_name="A")
    uid = user["id"]
    await user_repo.add_own_email(conn, uid, ME)
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "901", uid)
    agent, seen = triage_agent(5)

    async def handle(o: Observation) -> Observation:
        stored = await observation_repo.insert(conn, o)
        await triage.handle(stored, settings=settings, agent=agent, notifier=notifier)
        return stored

    # the person's own new event: kept, never triaged
    await handle(obs_of(uid, change(event_id="own", event_type="created", organizer_email=ME, attendees=[])))
    assert seen == [] and tg.sent == []
    # Ekin's invitation: triaged with what it is, notified
    await handle(obs_of(uid, change(event_type="created")))
    assert [s["payload"]["change"] for s in seen] == ["invited"]
    assert tg.sent[-1]["text"].startswith("‼️ Ekin Derin · calendar\nKickoff")
    # Google's invitation mail for it minutes later: already told, no model call, no second ping
    await handle(mail(uid, "inv", "Invitation: Kickoff @ Tue Sep 15, 2026 12pm - 1pm (GMT+2) (me@example.test)"))
    assert len(seen) == 1 and len(tg.sent) == 1

    # the other order: Google's mail about a moved meeting comes first, the calendar change after it
    await observation_repo.insert(conn, Observation(
        user_id=uid, source="calendar", source_key="snapshot:ev2", kind="event_snapshot", occurred_at=NOW - timedelta(days=1),
        thread_key="ev2", is_backfill=True, status="done",
        payload=gcal.sync_shape({"id": "ev2", "summary": "Review", "start": {"dateTime": "2026-09-16T09:00:00+02:00"},
                                 "end": {"dateTime": "2026-09-16T10:00:00+02:00"}, "organizer": {"email": EKIN, "displayName": "Ekin Derin"}})))
    await handle(mail(uid, "upd", "Updated invitation: Review @ Wed Sep 16, 2026 11am - 12pm (GMT+2) (me@example.test)"))
    assert len(seen) == 2 and len(tg.sent) == 2
    moved = await handle(obs_of(uid, change(event_id="ev2", summary="Review", start_time="2026-09-16T11:00:00+02:00",
                                            end_time="2026-09-16T12:00:00+02:00")))
    assert len(seen) == 2 and len(tg.sent) == 2
    kept = (await observation_repo.get(conn, moved.id)).payload     # what changed stays with it for the chat agent
    assert kept["change"] == "moved" and kept["was_start"] == "2026-09-16T09:00:00+02:00"
    # ordinary mail is untouched by all of this
    await handle(mail(uid, "invoice", "Invoice 2041"))
    assert len(seen) == 3


class FakeTriggers:
    def __init__(self, active):
        self.active, self.created, self.listed = list(active), [], []

    def list_active(self, *, connected_account_ids=None, show_disabled=None, **_):
        self.listed.append(connected_account_ids)
        return SimpleNamespace(items=[SimpleNamespace(trigger_name=n) for n in self.active])

    def create(self, slug, **kw):
        self.created.append((slug, kw))
        self.active.append(slug)
        return SimpleNamespace(trigger_id=f"ti_{len(self.created)}")


EVENTS = [
    {"id": "own1", "summary": "Gym", "status": "confirmed", "updated": ago(1), "start": {"dateTime": "2026-09-15T18:00:00+02:00"},
     "end": {"dateTime": "2026-09-15T19:00:00+02:00"}, "organizer": {"email": ME, "self": True}},
    {"id": "ev1", "summary": "Kickoff", "status": "confirmed", "updated": ago(2), "start": {"dateTime": "2026-09-15T12:00:00+02:00"},
     "end": {"dateTime": "2026-09-15T13:00:00+02:00"}, "organizer": {"email": EKIN, "displayName": "Ekin Derin"},
     "attendees": [{"email": ME, "self": True, "responseStatus": "needsAction"}, {"email": EKIN, "responseStatus": "accepted"}]},
    {"id": "old", "summary": "Old", "status": "cancelled", "updated": ago(5)},
]


class SyncAdapter(ComposioAdapter):
    """Real adapter code with a fake execute and a fake trigger client."""

    def __init__(self, settings, calls, triggers):
        super().__init__(settings=settings, execute=self.fake, client=SimpleNamespace(triggers=triggers))
        self.calls = calls

    def fake(self, slug, args, *, user_id=None):
        self.calls.append((slug, args))
        if slug == "GOOGLECALENDAR_GET_CALENDAR":
            return {"successful": True, "data": {"calendar_data": {"id": "Me@Example.test", "summary": "Me"}, "display_url": None}}
        if slug == "GOOGLECALENDAR_FIND_EVENT":
            return {"successful": True, "data": {"defaultReminders": [{"method": "popup", "minutes": 30}], "event_data": {"event_data": EVENTS}}}
        raise AssertionError(f"unexpected slug {slug}")


async def test_calendar_setup_once_idempotent_and_kept_out_of_chat(conn, settings):
    user = await user_repo.create(conn, control_source="telegram", control_thread_key="902")
    uid = user["id"]
    calls, triggers = [], FakeTriggers([STARTING])
    registry = AdapterRegistry({"composio": SyncAdapter(settings, calls, triggers), "telegram": FakeTelegram()})
    assert await calendar_sync.ensure(conn, uid, registry) is False and calls == []          # calendar not connected
    await user_repo.merge_state(conn, uid, {"connected": {"googlecalendar": "ca_cal"}})
    assert await calendar_sync.ensure(conn, uid, registry) is True
    assert [slug for slug, _ in triggers.created] == [SYNC]                                   # the existing trigger is left alone
    assert triggers.created[0][1]["connected_account_id"] == "ca_cal" and triggers.listed == [["ca_cal"]]
    user = await user_repo.get(conn, uid)
    assert user_repo.own_emails(user) == [ME] and user["state"]["calendar_sync"] == calendar_sync.SYNC_VERSION
    find = next(a for s, a in calls if s == "GOOGLECALENDAR_FIND_EVENT")
    assert find["max_results"] == 250 and find["single_events"] is True
    cur = await conn.execute("select thread_key, kind, status, is_backfill from observation where user_id = %s order by thread_key", (uid,))
    assert [tuple(r.values()) for r in await cur.fetchall()] == [("ev1", "event_snapshot", "done", True), ("own1", "event_snapshot", "done", True)]
    # done once: the next reconcile does nothing; a reconnect (force) repeats safely, without duplicates
    n = len(calls)
    assert await calendar_sync.ensure(conn, uid, registry) is False and len(calls) == n
    assert await calendar_sync.ensure(conn, uid, registry, force=True) is True
    assert [slug for slug, _ in triggers.created] == [SYNC] and await observation_repo.count(conn, uid, source="calendar") == 2
    # the baseline is state, not news: the chat agent never sees it...
    recent = await chat.recent_observations(conn, uid, settings, since=NOW - timedelta(days=3))
    assert all(r["kind"] != "event_snapshot" for r in recent)
    # ...but the first reported change of an event is compared with it
    moved = await observation_repo.insert(conn, obs_of(uid, change(start_time="2026-09-15T14:00:00+02:00", end_time="2026-09-15T15:00:00+02:00")))
    verdict = await pretriage.review(conn, moved, NOW)
    assert verdict.news and verdict.obs.payload["change"] == "moved" and verdict.obs.payload["was_start"] == "2026-09-15T12:00:00+02:00"


async def test_calendar_news_shares_the_daily_quota_with_mail(conn, settings):
    """What passes pretriage is gated like mail: one rolling daily quota for both, the reserve kept for urgency 5."""
    user = await user_repo.create(conn, control_source="telegram", control_thread_key="903")
    uid = user["id"]
    await user_repo.add_own_email(conn, uid, ME)
    tg = FakeTelegram()
    notifier = triage.Notifier(tg, "903", uid)
    normal, _ = triage_agent(4)
    urgent, _ = triage_agent(5)

    async def gate_reason(o: Observation, agent=normal) -> str | None:
        stored = await observation_repo.insert(conn, o)
        await triage.handle(stored, settings=settings, agent=agent, notifier=notifier)
        return ((await budget_repo.get_triage(conn, stored.id)) or {}).get("gate_reason")

    # defaults: 5 in 24 h, the last 2 kept for urgency 5 -> three ordinary notifications, whatever their source
    assert await gate_reason(mail(uid, "q1", "Invoice 1")) == "ok"
    assert await gate_reason(obs_of(uid, change(event_id="q-inv", event_type="created"))) == "ok"          # an invite takes a slot
    assert await gate_reason(mail(uid, "q2", "Invoice 2")) == "ok"
    assert await gate_reason(mail(uid, "q3", "Invoice 3")) == "quota_exhausted"                           # ...so this mail waits
    assert await gate_reason(obs_of(uid, change(event_id="q-gone", summary="Retro", event_type="deleted", status="cancelled"))) == "quota_exhausted"
    assert await gate_reason(obs_of(uid, change(event_id="q-new", summary="Board", event_type="created")), agent=urgent) == "urgency_bypass"
    # the person's own edits never reach the gate, so they cost nothing
    assert await gate_reason(obs_of(uid, change(event_id="q-own", event_type="created", organizer_email=ME, attendees=[]))) is None
    assert len(tg.sent) == 4
