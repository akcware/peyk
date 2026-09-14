"""Is the person free at the time a mail or an invitation asks about? calendar.check_time judges one day of events;
triage looks a second time with it, so the notification says whether they are free and a mail asking to meet comes
with a reply card; the chat agent's check_time tool reads the same answer. Ekin's test from 2026-09-14 is the model
case: lunch 12:00-13:00 Berlin, a meeting proposed for 13:00 while she is in Turkey. No LLM: fake agents."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from adapters.composio import calendar as gcal
from adapters.composio.adapter import ComposioAdapter
from agent.chat_agent import make_tools
from agent.client import AgentClient
from agent.schemas import TriageResult
from agent.triage_agent import render_prompt
from core.adapter import AdapterRegistry
from core.models import Observation
from core.repo import action_repo, budget_repo, observation_repo, user_repo
from tests.phase1.test_worker_flow import FakeTelegram
from workers import approval, chat, ticks, triage

TR, DE = "Europe/Istanbul", "Europe/Berlin"
NOW = datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo(TR))


def ev(eid: str, title: str, start: str, end: str, **extra) -> dict:
    return {"id": eid, "summary": title, "status": "confirmed", "start": {"dateTime": start}, "end": {"dateTime": end},
            "organizer": {"email": "me@example.test", "self": True}, **extra}


LUNCH = ev("lunch", "Lunch date with x", "2026-09-15T12:00:00+02:00", "2026-09-15T13:00:00+02:00")
EVENTS = [LUNCH,
          ev("gym", "Gym", "2026-09-15T15:00:00+02:00", "2026-09-15T16:00:00+02:00", transparency="transparent"),
          ev("no", "Declined thing", "2026-09-15T12:30:00+02:00", "2026-09-15T13:30:00+02:00", organizer={"email": "o@x.test"},
             attendees=[{"email": "me@example.test", "self": True, "responseStatus": "declined"}]),
          {"id": "bday", "summary": "Birthday", "status": "confirmed", "start": {"date": "2026-09-15"}, "end": {"date": "2026-09-16"}}]


def compact(events=EVENTS) -> list[dict]:
    return [gcal.compact_event(e) for e in events]


# ---------------- check_time ----------------

def test_a_clash_in_turkey_is_back_to_back_in_berlin():
    """The same lunch against a 13:00 meeting: on a Turkish clock it overlaps, on a Berlin clock it ends right before."""
    tr = gcal.check_time(compact(), "2026-09-15T13:00:00+03:00", tz=TR, now=NOW)
    assert tr["free"] is False and tr["asked"] == {"start": "2026-09-15T13:00:00+03:00", "end": "2026-09-15T14:00:00+03:00"}
    assert tr["overlaps"] == [{"title": "Lunch date with x", "start": "2026-09-15T13:00:00+03:00", "end": "2026-09-15T14:00:00+03:00"}]
    # the nearest free hours that day, in time order; the declined, the "show as free" and the all-day events never block
    assert [a["start"][11:16] for a in tr["alternatives"]] == ["11:30", "12:00", "14:00"]

    de = gcal.check_time(compact(), "2026-09-15T13:00:00+02:00", tz=DE, now=NOW)
    assert de["free"] is True and de["overlaps"] == [] and de["alternatives"] == []
    assert [e["title"] for e in de["right_before"]] == ["Lunch date with x"] and de["right_after"] == []


def test_check_time_edges():
    lunch = compact([LUNCH])
    # a meeting ending 10 minutes before lunch is back to back on the other side; 20 minutes is not near
    assert [e["title"] for e in gcal.check_time(lunch, "2026-09-15T11:00:00+02:00", "2026-09-15T11:50:00+02:00", tz=DE)["right_after"]] == ["Lunch date with x"]
    assert gcal.check_time(lunch, "2026-09-15T11:00:00+02:00", "2026-09-15T11:40:00+02:00", tz=DE)["right_after"] == []
    # the event being asked about is not its own clash (by id for calendar news, by title for Google's invitation mails)
    assert gcal.check_time(lunch, "2026-09-15T12:00:00+02:00", tz=DE, exclude_id="lunch")["free"] is True
    assert gcal.check_time(lunch, "2026-09-15T12:00:00+02:00", tz=DE, exclude_title="lunch date with x")["free"] is True
    # no alternative lies in the past, and an evening proposal stretches the searched part of the day
    late = gcal.check_time(lunch, "2026-09-15T12:30:00+02:00", tz=DE, now=datetime(2026, 9, 15, 13, 45, tzinfo=ZoneInfo(DE)))
    assert all(a["start"] >= "2026-09-15T13:45" for a in late["alternatives"])
    dinner = compact([ev("d", "Dinner", "2026-09-15T21:00:00+02:00", "2026-09-15T22:00:00+02:00")])
    assert [a["start"][11:16] for a in gcal.check_time(dinner, "2026-09-15T21:00:00+02:00", tz=DE)["alternatives"]] == ["19:00", "19:30", "20:00"]
    assert gcal.day_window("2026-09-15T23:30:00+03:00", "2026-09-16T00:30:00+03:00", TR) == (
        "2026-09-15T00:00:00+03:00", "2026-09-17T00:00:00+03:00")


def test_triage_prompt_carries_the_calendar_but_not_old_findings():
    check = gcal.check_time(compact(), "2026-09-15T13:00:00+03:00", tz=TR, now=NOW)
    payload = {"from": "Ekin <dekin031@gmail.com>", "subject": "Toplantı", "snippet": "Yarın 13.00da toplantımız var",
               "calendar_check": {"free": True}, "suggested_reply": "old"}
    p = render_prompt({"source": "gmail", "kind": "message_in", "occurred_at": NOW.isoformat(), "payload": payload,
                       "calendar_check": check}, None)
    assert "calendar_check" not in p and "suggested_reply" not in p
    tail = p.split("--- calendar at that time (read just now, times on the person's clock) ---")[1]
    assert "asked time: Tue 15 Sep 13:00-14:00" in tail and "free then: no" in tail
    assert "overlaps with: Lunch date with x (Tue 15 Sep 13:00-14:00)" in tail
    assert "free alternatives that day: Tue 15 Sep 11:30-12:30, Tue 15 Sep 12:00-13:00, Tue 15 Sep 14:00-15:00" in tail
    assert "--- calendar" not in render_prompt({"source": "gmail", "kind": "message_in", "occurred_at": "t", "payload": payload}, None)


# ---------------- the triage worker ----------------

# what Composio's managed Google project answered on 2026-09-14, the first time this ran in production
QUOTA = ('{\n  "error": {\n    "code": 403,\n    "message": "Quota exceeded for quota metric \'Queries\' and limit \'Queries '
         'per minute\' of service \'calendar-json.googleapis.com\' for consumer \'project_number:1\'."\n  }\n}')


def test_only_the_per_minute_quota_counts_as_rate_limited():
    assert gcal.rate_limited(RuntimeError(QUOTA)) and gcal.rate_limited(RuntimeError("HTTP 429 Too Many Requests"))
    assert gcal.rate_limited(RuntimeError("403 rateLimitExceeded"))
    assert not gcal.rate_limited(RuntimeError("401 Unauthorized: invalid_grant"))
    assert not gcal.rate_limited(RuntimeError("Quota exceeded for quota metric 'Queries' and limit 'Queries per day'"))
    assert not gcal.rate_limited(RuntimeError("event 14290 not found"))


class ClashAdapter(ComposioAdapter):
    def __init__(self, settings, calls, *, fails: int = 0, error: str = QUOTA):
        def execute(slug, args, *, user_id=None):
            calls.append((slug, args))
            if len(calls) <= fails:
                return {"successful": False, "error": error, "data": {}}
            assert slug == "GOOGLECALENDAR_FIND_EVENT"
            return {"successful": True, "data": {"event_data": {"event_data": EVENTS + [INVITED]}}}
        super().__init__(settings=settings, execute=execute)

    async def connected_toolkits(self, conn):
        return {"googlecalendar": "ca_c", "gmail": "ca_g"}


INVITED = ev("inv", "Sync", "2026-09-15T13:00:00+03:00", "2026-09-15T14:00:00+03:00", organizer={"email": "mara@example-client.test"})


def two_looks(seen: list, *, urgency: int = 4, second_urgency: int | None = None, second_fails: bool = False,
              reply: str = "13:00 olmuyor, öğle yemeğim var. 14:00 uyar mı?") -> AgentClient:
    """First look: a time is asked for (and, like the real model, a reply written blind). Second look (with the
    calendar): the summary names the clash, plus a reply; the real model files it under "calendar" as often as not."""
    def handle(payload):
        obs = payload["observation"]
        seen.append(obs)
        if "calendar_check" not in obs:
            r = TriageResult(urgency=urgency, category="person", reason="asks to meet", summary="Ekin asks to meet tomorrow at 13:00.",
                             proposed_start="2026-09-15T13:00:00+03:00", reply="Tamam, yarın 13:00'te oradayım.")
        elif second_fails:
            raise RuntimeError("bedrock throttled")
        else:
            r = TriageResult(urgency=second_urgency or urgency, category="calendar", reason="asks to meet, clash",
                             summary="Ekin asks to meet tomorrow at 13:00, but your lunch runs 13:00-14:00.", reply=reply)
        return {"task": "triage", "result": r.model_dump(), "model_id": "fake", "latency_ms": 1}
    return AgentClient("local", handle_fn=handle)


async def _setup(conn, settings, *, connected=True, fails: int = 0, error: str = QUOTA):
    calls: list = []
    tg = FakeTelegram()
    registry = AdapterRegistry({"composio": ClashAdapter(settings, calls, fails=fails, error=error), "telegram": tg})
    user, _ = await user_repo.get_or_create_by_control(conn, "telegram", "777", timezone=TR)
    if connected:
        await user_repo.merge_state(conn, user["id"], {"connected": {"gmail": "ca_g", "googlecalendar": "ca_c"}})
    notifier = triage.Notifier(tg, "777", user["id"])
    return user["id"], tg, registry, notifier, calls


def mail(uid, key: str, subject: str = "Toplantı", text: str = "Yarın saat 13.00da toplantımız var") -> Observation:
    return Observation(user_id=uid, source="gmail", source_key=key, kind="message_in", occurred_at=NOW.astimezone(UTC),
                       thread_key=f"th-{key}", payload={"from": "Ekin Derin <dekin031@gmail.com>", "subject": subject, "snippet": text})


async def _run(obs, settings, agent, notifier, registry):
    await triage.handle(obs, settings=settings, agent=agent, notifier=notifier, now=NOW.astimezone(UTC),
                        tick_ctx=ticks.TickContext(settings=settings, registry=registry, notifier=notifier),
                        approval=approval.ApprovalFlow(registry, notifier))


async def test_a_mail_asking_to_meet_is_checked_against_the_calendar_and_gets_a_reply_card(conn, settings):
    uid, tg, registry, notifier, calls = await _setup(conn, settings)
    obs = await observation_repo.insert(conn, mail(uid, "m1"))
    seen: list = []
    await _run(obs, settings, two_looks(seen), notifier, registry)

    assert len(seen) == 2 and "calendar_check" not in seen[0]
    assert [o["title"] for o in seen[1]["calendar_check"]["overlaps"]] == ["Lunch date with x", "Sync"]
    _, args = calls[0]      # one read: the person's whole day on their current clock
    assert len(calls) == 1 and args["time_min"] == "2026-09-15T00:00:00+03:00" and args["time_max"] == "2026-09-16T00:00:00+03:00"

    note, card = tg.sent
    assert note["text"].endswith("Ekin asks to meet tomorrow at 13:00, but your lunch runs 13:00-14:00.")
    assert card["text"] == "📝 Draft (reply in thread)\n\n13:00 olmuyor, öğle yemeğim var. 14:00 uyar mı?"
    assert [b["text"] for b in card["markup"]["inline_keyboard"][0]] == ["✅ Send", "✏️ Edit", "❌ Cancel"]
    action = (await action_repo.list_open(conn, uid))[0]
    assert action["channel"] == "gmail" and action["thread_key"] == "th-m1" and action["content"]["to"] == []

    stored = await observation_repo.get(conn, obs.id)
    assert stored.payload["calendar_check"]["free"] is False and stored.payload["suggested_reply"].startswith("13:00 olmuyor")
    row = await budget_repo.get_triage(conn, obs.id)
    assert row["summary"].endswith("13:00-14:00.") and row["latency_ms"] == 2 and row["gate_reason"] == "ok"
    # the chat agent sees what the calendar said when the mail came in
    recent = await chat.recent_observations(conn, uid, settings, since=NOW.astimezone(UTC).replace(hour=0))
    assert recent[0]["calendar_check"]["overlaps"][0]["title"] == "Lunch date with x" and recent[0]["suggested_reply"]


async def test_no_calendar_no_second_look(conn, settings):
    uid, tg, registry, notifier, calls = await _setup(conn, settings, connected=False)
    seen: list = []
    await _run(await observation_repo.insert(conn, mail(uid, "m2")), settings, two_looks(seen), notifier, registry)
    assert len(seen) == 1 and calls == [] and len(tg.sent) == 1
    assert tg.sent[0]["text"].endswith("Ekin asks to meet tomorrow at 13:00.")


async def test_googles_per_minute_quota_is_waited_out(conn, settings, monkeypatch):
    """Production, 2026-09-14: the first real check hit Google's per-minute quota and the notification went out
    without the calendar. The quota clears within a minute, so the check waits and tries again."""
    monkeypatch.setattr(triage, "CALENDAR_RETRY_WAITS", (0.0, 0.0))
    uid, tg, registry, notifier, calls = await _setup(conn, settings, fails=1)
    seen: list = []
    await _run(await observation_repo.insert(conn, mail(uid, "m7")), settings, two_looks(seen), notifier, registry)
    assert len(calls) == 2 and len(seen) == 2 and len(tg.sent) == 2       # read on the second try: clash told, card offered


async def test_an_unreadable_calendar_keeps_the_first_look(conn, settings, monkeypatch):
    monkeypatch.setattr(triage, "CALENDAR_RETRY_WAITS", (0.0, 0.0))
    uid, tg, registry, notifier, calls = await _setup(conn, settings, fails=99)
    seen: list = []
    obs = await observation_repo.insert(conn, mail(uid, "m3"))
    await _run(obs, settings, two_looks(seen), notifier, registry)
    assert len(seen) == 1 and len(calls) == 3 and len(tg.sent) == 1        # three tries; notified, no card, nothing guessed
    assert "calendar_check" not in (await observation_repo.get(conn, obs.id)).payload

    # any other error is not waited for: one try
    uid, tg, registry, notifier, calls = await _setup(conn, settings, fails=99, error="401 Unauthorized: invalid_grant")
    await _run(await observation_repo.insert(conn, mail(uid, "m8")), settings, two_looks([]), notifier, registry)
    assert len(calls) == 1 and len(tg.sent) == 1


async def test_a_failed_second_look_never_offers_the_blind_reply(conn, settings):
    """The first look's reply was written without the calendar ("I'll be there" while lunch is booked): if the second
    look fails, the person is still told, but nothing is offered for sending."""
    uid, tg, registry, notifier, _ = await _setup(conn, settings)
    seen: list = []
    obs = await observation_repo.insert(conn, mail(uid, "m6"))
    await _run(obs, settings, two_looks(seen, second_fails=True), notifier, registry)
    assert len(seen) == 2 and len(tg.sent) == 1 and tg.sent[0]["text"].endswith("Ekin asks to meet tomorrow at 13:00.")
    assert await action_repo.list_open(conn, uid) == []
    stored = (await observation_repo.get(conn, obs.id)).payload
    assert stored["calendar_check"]["free"] is False and "suggested_reply" not in stored


async def test_google_invitation_mail_names_other_clashes_but_gets_no_mail_reply(conn, settings):
    """An invitation is answered in the calendar, not by mail; the invited event itself is no clash."""
    uid, tg, registry, notifier, _ = await _setup(conn, settings)
    seen: list = []
    invite = mail(uid, "m4", subject="Invitation: Sync @ Tue 15 Sep 2026 1pm - 2pm (GMT+3) (me@example.test)", text="Mara invites you")
    await _run(await observation_repo.insert(conn, invite), settings, two_looks(seen), notifier, registry)
    assert [o["title"] for o in seen[1]["calendar_check"]["overlaps"]] == ["Lunch date with x"]    # not "Sync" itself
    assert len(tg.sent) == 1 and await action_repo.list_open(conn, uid) == []


async def test_held_back_mail_keeps_the_check_but_sends_no_card(conn, settings):
    """The second look may find it matters less (urgency 2): no ping, so no card either."""
    uid, tg, registry, notifier, _ = await _setup(conn, settings)
    seen: list = []
    obs = await observation_repo.insert(conn, mail(uid, "m5"))
    await _run(obs, settings, two_looks(seen, urgency=3, second_urgency=2), notifier, registry)
    assert len(seen) == 2 and tg.sent == [] and await action_repo.list_open(conn, uid) == []
    assert (await budget_repo.get_triage(conn, obs.id))["gate_reason"] == "below_threshold"
    assert (await observation_repo.get(conn, obs.id)).payload["suggested_reply"]      # the brief and the chat still have it


# ---------------- the chat tool ----------------

async def test_chat_check_time_reads_the_day_once_and_answers_on_the_persons_clock(conn, settings):
    uid, _, registry, _, calls = await _setup(conn, settings)
    intents: list = []
    ctx = {"timezone": TR, "user_state": {"connected": ["googlecalendar"]}}
    tools = {t.tool_name: t for t in make_tools(ctx, intents)}
    assert "ISO-8601" in tools["check_time"]._tool_func("yarın 13:00")["error"]
    assert tools["check_time"]._tool_func("2026-09-15T13:00:00+03:00")["note"].startswith("reading")
    q = intents[-1]
    assert q == {"intent": "CalendarQuery", "op": "check", "start": "2026-09-15T13:00:00+03:00", "end": "",
                 "key": "check|2026-09-15T13:00:00+03:00|"}

    store = {"events": {}, "free": {}}
    await chat.resolve_calendar([q], store, uid, registry, TR, now=NOW)
    assert len(calls) == 1 and calls[0][1]["time_min"] == "2026-09-15T00:00:00+03:00"
    answer = {t.tool_name: t for t in make_tools({**ctx, "calendar": store}, intents)}["check_time"]._tool_func("2026-09-15T13:00:00+03:00")
    assert answer["timezone"] == TR and answer["free"] is False and answer["overlaps"][0]["start"] == "2026-09-15T13:00:00+03:00"

    off = {t.tool_name: t for t in make_tools({"timezone": TR, "user_state": {"connected": ["gmail"]}}, [])}
    assert "not connected" in off["check_time"]._tool_func("2026-09-15T13:00:00+03:00")["error"]
