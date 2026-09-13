"""A traveller: lives in Berlin (profile zone Europe/Berlin, calendar events at +02:00) and is in Turkey now (UTC+3).
Calendar tools, cards, the chat context and notifications speak in the zone the person is in; saying where they
are moves it within the same turn, and only a real IANA zone is saved."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from adapters.composio import calendar as gcal
from agent.chat_agent import make_tools
from agent.client import AgentClient
from agent.triage_agent import render_prompt
from core.adapter import AdapterRegistry
from core.embeddings import FakeEmbedder
from core.models import Observation
from core.repo import observation_repo, user_repo
from tests.multiuser.test_calendar_tools import EVENT, CalAdapter
from tests.multiuser.test_multiuser import tg_text
from tests.phase1.test_worker_flow import FakeTelegram
from workers import actions, chat, triage

HOME, AWAY = "Europe/Berlin", "Europe/Istanbul"
DAY = ("2026-09-14T00:00:00+03:00", "2026-09-15T00:00:00+03:00")
KEY = f"{DAY[0]}|{DAY[1]}"


def _tools(tz: str, intents: list):
    cached = {"events": {f"{KEY}|": [gcal.compact_event(EVENT)]},
              "free": {KEY: {"busy": [{"start": "2026-09-14T15:00:00+02:00", "end": "2026-09-14T16:00:00+02:00"}], "free": []}}}
    ctx = {"timezone": tz, "user_state": {"connected": ["googlecalendar"]}, "calendar": cached}
    return {t.tool_name: t for t in make_tools(ctx, intents)}


def test_calendar_tools_speak_the_current_zone():
    tools = _tools(AWAY, [])
    found = tools["find_events"]._tool_func(*DAY)
    assert found["timezone"] == AWAY
    assert (found["events"][0]["start"], found["events"][0]["end"]) == ("2026-09-14T16:00:00+03:00", "2026-09-14T17:00:00+03:00")
    assert tools["find_free_time"]._tool_func(*DAY)["busy"] == [{"start": "2026-09-14T16:00:00+03:00", "end": "2026-09-14T17:00:00+03:00"}]


def test_saying_where_you_are_moves_the_zone_within_the_turn():
    intents: list = []
    tools = _tools(HOME, intents)
    assert tools["find_events"]._tool_func(*DAY)["events"][0]["start"] == "2026-09-14T15:00:00+02:00"      # still the Berlin clock
    assert "IANA" in tools["set_profile"]._tool_func(timezone="Turkey")["timezone_error"] and intents[-1]["timezone"] == ""
    assert tools["set_profile"]._tool_func(timezone=AWAY) == {"saved": True}
    assert intents[-1] == {"intent": "ProfileUpdate", "profile": "", "language": "", "timezone": AWAY, "display_name": ""}
    assert tools["find_events"]._tool_func(*DAY)["events"][0]["start"] == "2026-09-14T16:00:00+03:00"      # now the Turkish clock
    tools["create_event"]._tool_func("Ekin ile toplantı", "2026-09-15T12:00:00+03:00")
    assert intents[-1]["timezone"] == AWAY
    assert actions.event_done_line(intents[-1], "tr") == "📅 Ekin ile toplantı, 15 Eylül Salı, 12:00-13:00"
    # the card for moving a Berlin-kept meeting says both times on the Turkish clock
    tools["update_event"]._tool_func("ev1", start_iso="2026-09-14T18:00:00+03:00")
    assert actions.render_draft({"content": {"event": intents[-1]}}, "tr") == (
        "📅 Değişiklik\nMeeting with Boss\n14 Eylül Pazartesi, 18:00-19:00\nÖnceki: 14 Eylül Pazartesi, 16:00-17:00\n"
        "Davetliler: Ekin Derin\nDavetlilere bildirim gider.")


async def test_a_traveller_says_where_they_are(conn, settings):
    calls: list = []
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": CalAdapter(settings, calls), "telegram": tg})
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "777", timezone=HOME)
    uid = user["id"]
    seen = []

    def handle(payload):
        if payload["task"] == "chat_ack":
            return {"task": "chat_ack", "needs_work": True, "message": ""}
        seen.append((payload["timezone"], payload["now_iso"]))
        intents: list = []
        tools = {t.tool_name: t for t in make_tools(payload, intents)}
        tools["set_profile"]._tool_func(timezone=AWAY)
        day = tools["find_events"]._tool_func(*DAY)
        tools["find_events"]._tool_func(*DAY)                  # models repeat themselves; the calendar is read once
        if "note" in day:
            return {"task": "chat", "reply": "bakıyorum", "intents": intents}
        first = day["events"][0]
        return {"task": "chat", "reply": f"{first['title']} {first['start'][11:16]}", "intents": intents}

    msg = await observation_repo.insert(conn, tg_text("777", "31", "Bu hafta Türkiye'deyim, yarın neler var?", uid))
    reply = await chat.handle_message(conn, msg, settings=settings, agent=AgentClient("local", handle_fn=handle),
                                      notifier=triage.Notifier(tg, "777", uid), embedder=FakeEmbedder(), registry=registry)
    assert seen[0][0] == HOME and seen[1][0] == AWAY and seen[1][1].endswith("+03:00")      # round 2 already runs on Turkish time
    assert reply == "Meeting with Boss 16:00"
    assert [c[0] for c in calls] == ["GOOGLECALENDAR_FIND_EVENT"]
    assert (await user_repo.get(conn, uid))["timezone"] == AWAY


async def test_a_country_is_not_a_time_zone(conn):
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "778", timezone=HOME)
    obs = await observation_repo.insert(conn, tg_text("778", "41", "Türkiye'deyim", user["id"]))
    applied = await chat.apply_intents(conn, obs, [{"intent": "ProfileUpdate", "profile": "Travels a lot", "language": "",
                                                    "timezone": "Turkey", "display_name": ""}], embedder=FakeEmbedder())
    row = await user_repo.get(conn, user["id"])
    assert applied == ["ProfileUpdate"] and row["timezone"] == HOME and row["profile"] == "Travels a lot"


def test_notifications_and_chat_context_use_the_current_zone():
    change = {"summary": "Kickoff", "change": "moved", "start_time": "2026-09-15T12:00:00+02:00", "was_start": "2026-09-15T09:00:00+02:00"}
    prompt = render_prompt({"source": "calendar", "kind": "event_changed", "occurred_at": "2026-09-14T08:00:00+00:00",
                            "payload": change, "user": {"timezone": AWAY}}, None)
    assert "the person's time zone now: Europe/Istanbul" in prompt and "occurred_at: 2026-09-14T11:00:00+03:00" in prompt
    assert "start_time: 2026-09-15T13:00:00+03:00" in prompt and "was_start: 2026-09-15T10:00:00+03:00" in prompt
    assert "summary: Kickoff" in prompt and "change: moved" in prompt
    plain = render_prompt({"source": "calendar", "kind": "event_changed", "occurred_at": "2026-09-14T08:00:00+00:00", "payload": change}, None)
    assert "start_time: 2026-09-15T12:00:00+02:00" in plain and "time zone now" not in plain      # no zone known: nothing rewritten
    o = Observation(id=uuid4(), user_id=uuid4(), source="calendar", source_key="k", kind="event_changed",
                    occurred_at=datetime(2026, 9, 14, 8, tzinfo=UTC), thread_key="ev1", payload=change)
    d = chat.compact(o, tz=AWAY)
    assert (d["start_time"], d["was_start"], d["change"]) == ("2026-09-15T13:00:00+03:00", "2026-09-15T10:00:00+03:00", "moved")
    assert chat.compact(o)["start_time"] == "2026-09-15T12:00:00+02:00"
