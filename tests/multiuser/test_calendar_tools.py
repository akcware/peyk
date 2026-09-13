"""Google Calendar as a tool surface: read (events, free time) and write (create, update, delete, answer) through a
fake Composio execute; the chat round loop; direct vs. confirmed writes; the approval card; the brief's agenda;
first-learn. No LLM: the fake agent calls the real chat tools."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from adapters.composio import calendar as gcal
from adapters.composio.adapter import ComposioAdapter
from adapters.composio.profile import PROFILE_SAMPLERS
from agent.chat_agent import make_tools
from agent.client import AgentClient
from agent.learn_agent import render_facts
from core.adapter import AdapterRegistry
from core.embeddings import FakeEmbedder
from core.models import Connection, Content, Observation
from core.phrases import when_text
from core.repo import action_repo, observation_repo, user_repo
from tests.conftest import USER_ID
from tests.multiuser.test_multiuser import tg_text
from tests.phase1.test_worker_flow import FakeTelegram
from tests.phase4.test_approval_flow import _approve_obs
from workers import actions, approval, chat, ticks, triage
from workers.approval import send_failure_key

TZ = "Europe/Berlin"
DAY = ("2026-09-14T00:00:00+02:00", "2026-09-15T00:00:00+02:00")

# Google event resources as GOOGLECALENDAR_FIND_EVENT returns them (shape verified on 2026-09-13).
EVENT = {"id": "ev1", "summary": "Meeting with Boss", "status": "confirmed", "htmlLink": "https://calendar.google.com/event?eid=ev1",
         "start": {"dateTime": "2026-09-14T15:00:00+02:00", "timeZone": "Europe/Istanbul"},
         "end": {"dateTime": "2026-09-14T16:00:00+02:00", "timeZone": "Europe/Istanbul"},
         "organizer": {"email": "me@example.test", "self": True},
         "attendees": [{"email": "me@example.test", "self": True, "responseStatus": "accepted"},
                       {"email": "ekin@example.test", "displayName": "Ekin Derin", "responseStatus": "needsAction"}]}
PRIVATE = {"id": "ev2", "summary": "Gym", "status": "confirmed", "start": {"dateTime": "2026-09-14T18:00:00+02:00"},
           "end": {"dateTime": "2026-09-14T19:30:00+02:00"}, "organizer": {"email": "me@example.test", "self": True}}
INVITE = {"id": "ev3", "summary": "Kickoff", "status": "confirmed", "start": {"dateTime": "2026-09-15T10:00:00+02:00"},
          "end": {"dateTime": "2026-09-15T11:00:00+02:00"}, "organizer": {"email": "mara@example-client.test"},
          "attendees": [{"email": "me@example.test", "self": True, "responseStatus": "needsAction"},
                        {"email": "mara@example-client.test", "responseStatus": "accepted"}]}
CANCELLED = {"id": "ev4", "summary": "Old", "status": "cancelled", "start": {"dateTime": "2026-09-14T09:00:00+02:00"},
             "end": {"dateTime": "2026-09-14T10:00:00+02:00"}}


def fake_execute(calls, *, fail: str | None = None):
    def execute(slug, args, *, user_id=None):
        calls.append((slug, args, user_id))
        if slug == fail:
            return {"successful": False, "error": "404 Not Found: event deleted", "data": {}}
        if slug == "GOOGLECALENDAR_FIND_EVENT":   # the list sits two levels down, next to the calendar's defaultReminders
            return {"successful": True, "data": {"kind": "calendar#events", "defaultReminders": [{"method": "popup", "minutes": 30}],
                                                 "event_data": {"event_data": [EVENT, PRIVATE, INVITE, CANCELLED], "nextPageToken": None}}}
        if slug == "GOOGLECALENDAR_FIND_FREE_SLOTS":
            return {"successful": True, "data": {"kind": "calendar#freeBusy", "calendars": {"primary": {
                "busy": [{"start": "2026-09-14T15:00:00+02:00", "end": "2026-09-14T16:00:00+02:00"}],
                "free": [{"start": "2026-09-14T08:00:00+02:00", "end": "2026-09-14T15:00:00+02:00"}]}}}}
        if slug == "GOOGLECALENDAR_CREATE_EVENT":
            return {"successful": True, "data": {"response_data": {"id": "new1", "htmlLink": "https://calendar.google.com/event?eid=new1"}}}
        if slug == "GOOGLECALENDAR_PATCH_EVENT":
            eid = args["event_id"]
            return {"successful": True, "data": {"response_data": {"id": eid, "htmlLink": f"https://calendar.google.com/event?eid={eid}"}}}
        if slug == "GOOGLECALENDAR_DELETE_EVENT":
            return {"successful": True, "data": {}}
        raise AssertionError(f"unexpected slug {slug}")
    return execute


class CalAdapter(ComposioAdapter):
    """Real adapter code with fake execute and a fake list of connected toolkits."""

    def __init__(self, settings, calls, *, connected=None, fail=None):
        super().__init__(settings=settings, execute=fake_execute(calls, fail=fail))
        self._connected = {"googlecalendar": "ca_c"} if connected is None else connected

    async def connected_toolkits(self, conn):
        return dict(self._connected)


def _conn() -> Connection:
    return Connection(adapter_id="composio", user_id=USER_ID, data={"composio_user_id": "u-1"})


def _ctx(**extra) -> dict:
    return {"timezone": TZ, "user_state": {"connected": ["googlecalendar"]}, **extra}


def _reject_obs(user_id, action: dict, tg_mid: int) -> Observation:
    data = actions.buttons(action)["inline_keyboard"][0][1]["callback_data"]
    assert data.startswith("act:reject:")
    return Observation(user_id=user_id, source="telegram", source_key=f"r{tg_mid}", kind="message_in", occurred_at=datetime.now(tz=UTC),
                       thread_key="777", payload={"update_id": 3, "callback_query": {"id": "cq-r", "data": data,
                                                                                     "message": {"message_id": tg_mid, "chat": {"id": 777}}}})


def test_when_text_speaks_the_persons_language():
    s, e = "2026-09-14T12:00:00+02:00", "2026-09-14T13:00:00+02:00"
    assert when_text(s, e, TZ, "tr") == "14 Eylül Pazartesi, 12:00-13:00"
    assert when_text(s, e, TZ, "en") == "Mon 14 Sep, 12:00-13:00"
    assert when_text(s, e, TZ, "de") == "Mo. 14. Sep., 12:00-13:00"
    assert when_text("2026-09-14T10:00:00Z", "", TZ, "en") == "Mon 14 Sep, 12:00"            # UTC read in the person's zone
    assert when_text("2026-09-14", "2026-09-15", TZ, "tr") == "14 Eylül Pazartesi, tüm gün"
    assert when_text("2026-09-14T23:00:00+02:00", "2026-09-15T01:00:00+02:00", TZ, "en") == "Mon 14 Sep 23:00 - Tue 15 Sep 01:00"
    assert when_text("yarın", "", TZ, "tr") == "yarın"


async def test_read_and_write_arguments(settings):
    calls = []
    a = CalAdapter(settings, calls)
    conn = _conn()
    events = await a.calendar_events(conn, *DAY, tz=TZ)
    assert [e["id"] for e in events] == ["ev1", "ev2", "ev3"]           # cancelled one dropped, reminders never read as events
    boss = events[0]
    assert boss["title"] == "Meeting with Boss" and boss["start"] == "2026-09-14T15:00:00+02:00" and boss["organized_by_me"]
    assert boss["guests"] == [{"email": "ekin@example.test", "name": "Ekin Derin", "response": "needsAction"}]
    assert boss["my_response"] == "accepted" and events[2]["organized_by_me"] is False and events[2]["my_response"] == "needsAction"
    slug, args, uid = calls[-1]
    assert slug == "GOOGLECALENDAR_FIND_EVENT" and uid == "u-1" and "query" not in args
    assert args["calendar_id"] == "primary" and args["single_events"] is True and args["time_min"] == DAY[0]

    free = await a.calendar_free(conn, "2026-09-14T08:00:00+02:00", "2026-09-14T18:00:00+02:00", TZ)
    assert free["busy"] == [{"start": "2026-09-14T15:00:00+02:00", "end": "2026-09-14T16:00:00+02:00"}] and len(free["free"]) == 1
    assert calls[-1][1]["items"] == ["primary"] and calls[-1][1]["timezone"] == TZ

    done = await a.calendar_write(conn, {"op": "create", "title": "Ekin ile toplantı", "start": "2026-09-14T10:00:00Z", "end": "",
                                         "attendees": [], "timezone": TZ})
    assert done == {"id": "new1", "url": "https://calendar.google.com/event?eid=new1"}
    args = calls[-1][1]
    assert args["start_datetime"] == "2026-09-14T12:00:00" and args["end_datetime"] == "2026-09-14T13:00:00" and args["timezone"] == TZ
    assert args["create_meeting_room"] is False and args["send_updates"] == "none" and "attendees" not in args

    await a.calendar_write(conn, {"op": "create", "title": "Sync", "start": "2026-09-14T12:00:00+02:00", "end": "2026-09-14T12:30:00+02:00",
                                  "attendees": ["ekin@example.test"], "meet": True, "timezone": TZ})
    args = calls[-1][1]
    assert args["attendees"] == ["ekin@example.test"] and args["send_updates"] == "all" and args["create_meeting_room"] is True
    assert args["end_datetime"] == "2026-09-14T12:30:00"

    await a.calendar_write(conn, {"op": "update", "event_id": "ev1", "start": "2026-09-14T17:00:00+02:00", "end": "2026-09-14T18:00:00+02:00",
                                  "attendees": [], "attendees_given": False, "timezone": TZ})
    slug, args, _ = calls[-1]
    assert slug == "GOOGLECALENDAR_PATCH_EVENT" and args["event_id"] == "ev1" and args["start_time"] == "2026-09-14T17:00:00+02:00"
    assert "attendees" not in args and "summary" not in args and args["send_updates"] == "all"

    await a.calendar_write(conn, {"op": "delete", "event_id": "ev1"})
    assert calls[-1][0] == "GOOGLECALENDAR_DELETE_EVENT" and calls[-1][1]["event_id"] == "ev1"
    await a.calendar_write(conn, {"op": "rsvp", "event_id": "ev3", "response": "accepted"})
    assert calls[-1][1]["rsvp_response"] == "accepted"
    with pytest.raises(ValueError):
        await a.calendar_write(conn, {"op": "rsvp", "event_id": "ev3", "response": "sure"})
    # an approved card goes out through send(); the event rides in content.extra
    ext = await a.send(conn, "", Content(text="", extra={"channel": "calendar", "event": {
        "op": "create", "title": "X", "start": "2026-09-14T12:00:00+02:00", "timezone": TZ}}))
    assert ext == "new1"


def test_calendar_tools_and_confirmation_policy():
    intents: list = []
    off = {t.tool_name: t for t in make_tools({"timezone": TZ, "user_state": {"connected": ["gmail"]}}, intents)}
    assert "not connected" in off["find_events"]._tool_func(*DAY)["error"] and intents == []
    assert "not connected" in off["create_event"]._tool_func("x", "2026-09-14T12:00:00+02:00")["error"] and intents == []

    tools = {t.tool_name: t for t in make_tools(_ctx(), intents)}
    assert tools["find_events"]._tool_func(*DAY)["note"].startswith("reading")
    assert intents[-1] == {"intent": "CalendarQuery", "op": "events", "start": DAY[0], "end": DAY[1], "query": "", "key": f"{DAY[0]}|{DAY[1]}|"}
    tools["find_free_time"]._tool_func(*DAY)
    assert intents[-1]["op"] == "free" and intents[-1]["key"] == f"{DAY[0]}|{DAY[1]}"
    assert "ISO-8601" in tools["find_events"]._tool_func("tomorrow", DAY[1])["error"]

    compact = [gcal.compact_event(e) for e in (EVENT, PRIVATE, INVITE)]
    intents.clear()
    tools = {t.tool_name: t for t in make_tools(_ctx(calendar={"events": {f"{DAY[0]}|{DAY[1]}|": compact}, "free": {}}), intents)}
    assert [e["id"] for e in tools["find_events"]._tool_func(*DAY)["events"]] == ["ev1", "ev2", "ev3"] and intents == []

    # a private event goes straight in; one with a guest becomes a card. The tool and the worker agree.
    assert tools["create_event"]._tool_func("Gym", "2026-09-14T07:00:00+02:00")["status"].startswith("done")
    assert intents[-1]["timezone"] == TZ and intents[-1]["attendees"] == [] and not actions.calendar_needs_confirmation(intents[-1])
    assert "card" in tools["create_event"]._tool_func("Sync", "2026-09-14T12:00:00+02:00", guests="ekin@example.test")["status"]
    assert actions.calendar_needs_confirmation(intents[-1])
    assert "after" in tools["create_event"]._tool_func("X", "2026-09-14T12:00:00+02:00", "2026-09-14T11:00:00+02:00")["error"]

    # moving the private gym keeps its 90 minutes and goes straight in; moving the meeting with a guest needs a tap
    assert "unknown event_id" in tools["update_event"]._tool_func("nope", start_iso="2026-09-14T19:00:00+02:00")["error"]
    assert tools["update_event"]._tool_func("ev2", start_iso="2026-09-14T20:00:00+02:00")["status"].startswith("done")
    assert intents[-1]["end"] == "2026-09-14T21:30:00+02:00" and intents[-1]["current"]["title"] == "Gym"
    assert not actions.calendar_needs_confirmation(intents[-1])
    assert "card" in tools["update_event"]._tool_func("ev1", start_iso="2026-09-14T17:00:00+02:00")["status"]
    assert actions.calendar_needs_confirmation(intents[-1])
    tools["update_event"]._tool_func("ev2", guests="ekin@example.test")         # adding a guest tells someone
    assert actions.calendar_needs_confirmation(intents[-1])
    # cancelling and answering always wait for a tap; the person's own event has no invitation to answer
    tools["cancel_event"]._tool_func("ev2")
    assert intents[-1]["op"] == "delete" and actions.calendar_needs_confirmation(intents[-1])
    assert "own event" in tools["respond_to_invite"]._tool_func("ev1", "accepted")["error"]
    assert "tentative" in tools["respond_to_invite"]._tool_func("ev3", "sure")["error"]
    tools["respond_to_invite"]._tool_func("ev3", "accepted")
    assert intents[-1]["op"] == "rsvp" and intents[-1]["response"] == "accepted" and actions.calendar_needs_confirmation(intents[-1])


async def test_chat_writes_private_events_directly_and_invites_through_a_card(conn, settings):
    calls = []
    a = CalAdapter(settings, calls)
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": a, "telegram": tg})
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "777", timezone=TZ)   # a fresh row, not USER_ID
    uid = user["id"]
    notifier = triage.Notifier(tg, "777", uid)
    flow = approval.ApprovalFlow(registry, notifier)
    seen = []

    def agent(guests: str) -> AgentClient:
        def handle(payload):
            if payload["task"] == "chat_ack":
                return {"task": "chat_ack", "needs_work": True, "message": ""}
            intents: list = []
            tools = {t.tool_name: t for t in make_tools(payload, intents)}
            day = tools["find_events"]._tool_func(*DAY)
            if "note" in day:
                return {"task": "chat", "reply": "bakıyorum", "intents": intents}
            seen.append([e["id"] for e in day["events"]])
            out = tools["create_event"]._tool_func("Ekin ile toplantı", "2026-09-14T12:00:00+02:00", guests=guests)
            return {"task": "chat", "reply": "Ekledim." if out["status"].startswith("done") else "Davet hazır.", "intents": intents}
        return AgentClient("local", handle_fn=handle)

    msg = await observation_repo.insert(conn, tg_text("777", "11", "yarın 12'ye Ekin ile toplantı koy", uid))
    await chat.handle_message(conn, msg, settings=settings, agent=agent(""), notifier=notifier, embedder=FakeEmbedder(),
                              registry=registry, on_draft=flow.on_draft)
    assert seen == [["ev1", "ev2", "ev3"]]                                   # the day was looked at before writing
    assert [c[0] for c in calls] == ["GOOGLECALENDAR_FIND_EVENT", "GOOGLECALENDAR_CREATE_EVENT"]
    assert [m["text"] for m in tg.sent] == ["Ekledim.", "📅 Ekin ile toplantı, Mon 14 Sep, 12:00-13:00\nhttps://calendar.google.com/event?eid=new1"]

    # with a guest: nothing is written before the tap, the card says who gets told
    tg.sent.clear()
    calls.clear()
    msg2 = await observation_repo.insert(conn, tg_text("777", "12", "Ekin'i de davet et", uid))
    await chat.handle_message(conn, msg2, settings=settings, agent=agent("ekin@example.test"), notifier=notifier, embedder=FakeEmbedder(),
                              registry=registry, on_draft=flow.on_draft)
    assert [c[0] for c in calls] == ["GOOGLECALENDAR_FIND_EVENT"]
    card = tg.sent[-1]
    assert card["text"] == "📅 New event\nEkin ile toplantı\nMon 14 Sep, 12:00-13:00\nGuests: ekin@example.test\nGuests get notified."
    assert [b["text"] for b in card["markup"]["inline_keyboard"][0]] == ["✅ Add", "❌ Cancel"]
    action = (await action_repo.list_open(conn, uid))[0]
    assert action["channel"] == "calendar" and action["content"]["event"]["attendees"] == ["ekin@example.test"]

    assert await flow.on_callback(conn, _approve_obs(uid, "777", action, card["message_id"])) == "Done"
    slug, args, _ = calls[-1]
    assert slug == "GOOGLECALENDAR_CREATE_EVENT" and args["attendees"] == ["ekin@example.test"] and args["send_updates"] == "all"
    assert tg.sent[-1]["text"] == "Added to your calendar 👍\nEkin ile toplantı, Mon 14 Sep, 12:00-13:00"
    assert (await action_repo.get(conn, action["id"]))["status"] == "sent"


async def test_failed_and_rejected_calendar_cards(conn, settings):
    calls = []
    a = CalAdapter(settings, calls, fail="GOOGLECALENDAR_DELETE_EVENT")
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": a, "telegram": tg})
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "777")
    uid = user["id"]
    flow = approval.ApprovalFlow(registry, triage.Notifier(tg, "777", uid))
    src = await observation_repo.insert(conn, tg_text("777", "21", "iptal et", uid))
    delete = {"op": "delete", "event_id": "ev1", "timezone": TZ, "current": gcal.compact_event(EVENT)}
    action = await flow.on_draft(conn, src, {"channel": "calendar", "subject": "Meeting with Boss", "body": "", "to": [], "event": delete})
    card = tg.sent[-1]
    assert card["text"] == "📅 Delete event\nMeeting with Boss\nMon 14 Sep, 15:00-16:00\nGuests get notified."
    assert await flow.on_callback(conn, _approve_obs(uid, "777", action, card["message_id"])) == "Couldn't send"
    assert tg.sent[-1]["text"] == "⚠️ I can't find that event anymore, it may have been deleted."
    # the person gives up on it: the card says the calendar was left alone
    again = await action_repo.get(conn, action["id"])
    assert await flow.on_callback(conn, _reject_obs(uid, again, card["message_id"])) == "Cancelled"
    assert tg.sent[-1]["text"] == "Okay, I left your calendar as it is."


def test_calendar_cards_in_turkish_and_failure_texts():
    current = gcal.compact_event(EVENT)
    move = {"op": "update", "event_id": "ev1", "start": "2026-09-14T17:00:00+02:00", "end": "2026-09-14T18:00:00+02:00",
            "attendees": [], "attendees_given": False, "timezone": TZ, "current": current}
    assert actions.render_draft({"content": {"event": move}}, "tr") == (
        "📅 Değişiklik\nMeeting with Boss\n14 Eylül Pazartesi, 17:00-18:00\nÖnceki: 14 Eylül Pazartesi, 15:00-16:00\n"
        "Davetliler: Ekin Derin\nDavetlilere bildirim gider.")
    delete = {"op": "delete", "event_id": "ev1", "timezone": TZ, "current": current}
    assert actions.render_draft({"content": {"event": delete}}, "tr") == (
        "📅 Etkinliği sil\nMeeting with Boss\n14 Eylül Pazartesi, 15:00-16:00\nDavetlilere bildirim gider.")
    rsvp = {"op": "rsvp", "event_id": "ev3", "response": "declined", "timezone": TZ, "current": gcal.compact_event(INVITE)}
    assert actions.render_draft({"content": {"event": rsvp}}, "tr") == "📅 Davete cevap\nKickoff\n15 Eylül Salı, 10:00-11:00\nCevap: Katılamıyorum"
    fake = {"id": uuid4(), "content_hash": "ab" * 32, "content": {"event": delete}}
    assert [b["text"] for b in actions.buttons(fake, "tr")["inline_keyboard"][0]] == ["✅ Sil", "❌ Vazgeç"]
    assert actions.render_done({"content": {"event": delete}}, "tr") == "Takvimden sildim.\nMeeting with Boss, 14 Eylül Pazartesi, 15:00-16:00"
    gym = {**move, "current": gcal.compact_event(PRIVATE)}
    assert actions.event_done_line(gym, "tr", url="https://x") == "Değiştirdim 👍\n📅 Gym, 14 Eylül Pazartesi, 17:00-18:00\nhttps://x"
    assert send_failure_key(RuntimeError("404 Not Found: event deleted"), calendar=True) == "cal_failed_missing"
    assert send_failure_key(RuntimeError("Connected account ca_x is in EXPIRED state"), calendar=True) == "cal_failed_auth"
    assert send_failure_key(RuntimeError("boom"), calendar=True) == "cal_failed"
    assert send_failure_key(RuntimeError("boom")) == "send_failed"


async def test_morning_brief_carries_todays_calendar(conn, settings):
    calls = []
    registry = AdapterRegistry({"composio": CalAdapter(settings, calls), "telegram": FakeTelegram()})
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "777")
    uid = user["id"]
    now = datetime(2026, 9, 14, 8, 0, tzinfo=ZoneInfo(TZ))
    assert await ticks.today_agenda(conn, uid, registry, now) == [] and calls == []    # not connected: no call at all
    await user_repo.merge_state(conn, uid, {"connected": {"googlecalendar": "ca_c"}})
    agenda = await ticks.today_agenda(conn, uid, registry, now)
    assert [e["id"] for e in agenda] == ["ev1", "ev2", "ev3"]
    assert calls[-1][1]["time_min"] == "2026-09-14T00:00:00+02:00" and calls[-1][1]["time_max"] == "2026-09-15T00:00:00+02:00"

    quiet = ticks.brief_event_text([], now, agenda)
    assert "nothing with urgency >= 3" in quiet and "- 15:00 Meeting with Boss (with Ekin Derin)" in quiet and "- 18:00 Gym" in quiet
    assert ticks.brief_event_text([], now) == ticks.brief_event_text([], now, [])          # no calendar: the old quiet line
    item = {"source": "gmail", "payload": {"from": "Mara", "subject": "Invoice"}, "urgency": 4, "occurred_at": now,
            "notified_at": None, "summary": "Invoice due Friday", "reason": "r"}
    busy = ticks.brief_event_text([item], now, agenda).splitlines()
    assert busy[1].startswith("- [gmail] u4") and busy[2] == "today's calendar (the person's own schedule, not news):"
    assert busy[3] == "- 15:00 Meeting with Boss (with Ekin Derin)" and busy[-1].startswith("Weave today's calendar")

    fallback = ticks.render_brief([], now, "tr", agenda)
    assert fallback.startswith("Günaydın ☀️ Dünden beri önemli bir şey gelmedi.\n\nBugün takviminde:\n📅 15:00 Meeting with Boss\n📅 18:00 Gym")
    assert ticks.render_brief([], now, "tr") == ticks.render_brief([], now, "tr", [])       # quiet stays quiet


def test_first_learn_samples_the_calendar(settings):
    calls = []
    a = CalAdapter(settings, calls)
    facts = PROFILE_SAMPLERS["googlecalendar"](a._execute, "u-1")
    assert calls[0][1]["max_results"] == 80
    assert {"kind": "self", "email": "me@example.test"} in facts
    contacts = {f["email"]: f for f in facts if f["kind"] == "contact"}
    assert contacts["ekin@example.test"]["name"] == "Ekin Derin" and contacts["mara@example-client.test"]["count"] == 1
    assert [f["title"] for f in facts if f["kind"] == "event"] == ["Meeting with Boss", "Gym", "Kickoff"]
    text = render_facts("googlecalendar", facts)
    assert "- Meeting with Boss — 2026-09-14T15:00 — 1 guests — theirs" in text
    assert "- Ekin Derin <ekin@example.test> meets x1" in text
