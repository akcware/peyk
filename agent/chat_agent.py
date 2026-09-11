"""Chat agent (Strands, tools). Read tools search data the workers pre-fetched into the payload;
side-effect tools return *intents* that workers apply. The agent never touches the DB or sends anything.

Payload:  {"task": "chat", "message": str, "history": [{"role": "user"|"assistant", "text": str}],
           "recent_observations": [...compact dicts...], "memory_hits": [{"text", "score"}], "now_iso": str}
Returns:  {"reply": str, "intents": [ {"intent": "MemoryWrite"|"ScheduleRequest"|"ActionDraft"|"NeedMore", ...} ]}
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from strands import Agent, tool

from agent.model import build_model, user_language, user_profile
from agent.schemas import ChatAck

CHAT_SYSTEM_PROMPT = """You are a personal proactive assistant for one person, reachable through Telegram.

About the person:
{profile}

Now: {now}

Account state:
{user_state}

Onboarding — you run it yourself, conversationally, no commands:
- If this is the first conversation or nothing is connected: greet briefly, say what you do (watch their mail and
  calendar, ping them only when it matters, answer questions, draft replies), then offer to connect Gmail and/or
  Google Calendar. When they agree, call connect_service for each service they want — you will get a link back
  to show them. Do not ask for their email address or password; the link handles login.
- If the profile is not set: ask, in one short question, who they are / what they do / what counts as urgent for
  them, and save the answer with set_profile (also their language and timezone if you can infer them).
- At any later time the person may ask to connect or disconnect a service; use connect_service.
- Keep onboarding to a few messages; never lecture.

You can see (via tools) the person's recent observations — emails, calendar events, messages — and a small
long-term memory. Rules:
- Answer briefly, in the language the person writes in (their default language is {language}). Telegram formatting:
  plain text, short lines.
- Use search_observations before claiming who wrote or what happened; quote sender and subject.
- If the answer needs data older or different from what search_observations returns, call need_more ONCE
  with a precise query; you will be re-run with more data.
- When the person states a durable fact about themselves, their preferences or their projects, call remember.
- When they ask to be reminded, call schedule_followup with an ISO timestamp (use the timezone in Now).
- When they ask you to write/reply to someone, call draft_reply; say that a draft is ready for approval.
  Never claim something was sent.
- Notifications you already sent appear in this conversation as your own messages, marked with the observation id.
  "This mail" / "bu mail" / "that one" means the observation you most recently notified about, unless the person
  says otherwise. Observations carry `notified_at` when you already told the person about them.
- "Send/forward this mail to X" means: call draft_reply with to=X and a body that conveys the mail's content in your
  own words (or quotes it) — a draft for approval, never a promise that it was sent.
- When you need someone's address, call find_contact(name) first; only ask the person if the lookup finds nothing.
  If find_contact returns nothing on the first try you will be re-run with lookup results — do not ask yet.
- Do not invent observations. If nothing matches, say so."""


ACK_SYSTEM_PROMPT = """You are the first reflex of a personal assistant chatting with one person on Telegram, like a good
secretary who answers immediately and naturally.

About the person:
{profile}

Now: {now}

Decide two things for the incoming message:
- needs_work: true if a proper answer requires looking at their emails, calendar, messages, long-term memory,
  drafting a message for them, or scheduling a reminder. false for greetings, small talk, arithmetic, general
  knowledge, or anything you can answer right away from the conversation itself.
- message: what to say right now, in the language the person writes in. If needs_work is true, one short,
  natural sentence that says what you are about to check (e.g. "Tabii, bugün gelen maillere hemen bakıyorum.").
  The person's default language is {language}.
  Do NOT answer the question yet in that case. If needs_work is false, this IS the full reply — keep it short.
Never mention tools, systems or that you are an AI."""


@lru_cache
def _ack_model():
    return build_model("triage", temperature=0.3, max_tokens=200)   # fast model


def acknowledge(payload: dict[str, Any]) -> dict[str, Any]:
    user = payload.get("user") or {}
    agent = Agent(
        model=_ack_model(),
        system_prompt=ACK_SYSTEM_PROMPT.format(profile=user_profile(payload), now=payload.get("now_iso", ""),
                                               language=user_language(user.get("language"))),
        messages=to_messages((payload.get("history") or [])[-4:]),
        callback_handler=None,
    )
    result = agent(str(payload.get("message") or ""), structured_output_model=ChatAck)
    ack: ChatAck = result.structured_output
    return {"needs_work": ack.needs_work, "message": ack.message.strip()}


def _match(obs: dict[str, Any], query: str, source: str | None) -> bool:
    if source and obs.get("source") != source:
        return False
    hay = " ".join(str(obs.get(k) or "") for k in ("from", "subject", "snippet", "text", "summary", "source")).lower()
    return all(term in hay for term in query.lower().split()) if query.strip() else True


def make_tools(ctx: dict[str, Any], intents: list[dict[str, Any]]) -> list[Any]:
    recent: list[dict[str, Any]] = ctx.get("recent_observations") or []
    memory: list[dict[str, Any]] = ctx.get("memory_hits") or []
    contacts: list[dict[str, Any]] = ctx.get("contacts") or []

    @tool
    def search_observations(query: str, source: str = "", since_iso: str = "") -> list[dict]:
        """Search the person's recent observations (emails, calendar events, messages) already loaded for this chat.

        Args:
            query: words to match against sender, subject and text; empty string returns the most recent ones
            source: optional filter: gmail | calendar | whatsapp | telegram
            since_iso: optional ISO timestamp; only observations at or after it
        """
        out = [o for o in recent if _match(o, query, source or None)]
        if since_iso:
            out = [o for o in out if str(o.get("occurred_at", "")) >= since_iso]
        return out[:12]

    @tool
    def search_memory(query: str) -> list[dict]:
        """Search long-term memory facts about the person (already retrieved for this chat).

        Args:
            query: what to look for
        """
        terms = query.lower().split()
        ranked = sorted(memory, key=lambda m: -sum(t in m.get("text", "").lower() for t in terms))
        return [{"text": m["text"], "score": m.get("score")} for m in ranked[:5]]

    @tool
    def remember(text: str) -> dict:
        """Store a durable fact about the person for later conversations.

        Args:
            text: the fact, one sentence, third person (e.g. "Uses Postgres in the proactive-agent project")
        """
        intents.append({"intent": "MemoryWrite", "text": text})
        return {"stored": True}

    @tool
    def schedule_followup(when_iso: str, note: str) -> dict:
        """Schedule a reminder for the person.

        Args:
            when_iso: ISO-8601 timestamp with timezone offset, e.g. 2026-09-11T09:00:00+02:00
            note: what to remind them about
        """
        datetime.fromisoformat(when_iso)
        intents.append({"intent": "ScheduleRequest", "when_iso": when_iso, "note": note})
        return {"scheduled": True, "when_iso": when_iso}

    @tool
    def draft_reply(body: str, thread_key: str = "", to: str = "", subject: str = "", channel: str = "gmail") -> dict:
        """Prepare a message draft for the person to approve before anything is sent.

        Args:
            body: the message text
            thread_key: the thread id to reply in (from an observation), empty for a new message
            to: recipient address(es), comma separated, for a new message
            subject: subject for a new message
            channel: gmail (default) or another channel the person mentioned
        """
        intents.append({"intent": "ActionDraft", "channel": channel or "gmail", "thread_key": thread_key or None,
                        "to": [t.strip() for t in to.split(",") if t.strip()], "subject": subject or None, "body": body})
        return {"draft": True}

    @tool
    def find_contact(name: str) -> dict:
        """Look up a person's email address (or phone) by name in the person's contacts and mail history.
        Call this BEFORE asking the person for an address.

        Args:
            name: the person's name as written, e.g. "Deniz Ateş"
        """
        terms = name.lower().split()
        hits = [c for c in contacts if all(t in (str(c.get("name", "")) + " " + str(c.get("email", ""))).lower() for t in terms)] or contacts
        if hits:
            return {"contacts": hits[:8]}
        intents.append({"intent": "FindContact", "name": name})
        return {"contacts": [], "note": "lookup requested; you will be re-run with the results"}

    @tool
    def connect_service(service: str) -> dict:
        """Start connecting an external service for the person (OAuth link is generated and shown to them).

        Args:
            service: gmail or googlecalendar
        """
        intents.append({"intent": "ConnectRequest", "service": service})
        return {"requested": True, "note": "a login link will be sent to the person right after your reply"}

    @tool
    def set_profile(profile: str = "", language: str = "", timezone: str = "", display_name: str = "") -> dict:
        """Save what you learned about the person: a short profile (who they are, what is urgent for them),
        their language (ISO code like tr, en, de), IANA timezone (e.g. Europe/Berlin) and how to address them.

        Args:
            profile: 1-3 sentences, third person
            language: ISO 639-1 code
            timezone: IANA timezone name
            display_name: how to address the person
        """
        intents.append({"intent": "ProfileUpdate", "profile": profile, "language": language, "timezone": timezone,
                        "display_name": display_name})
        return {"saved": True}

    @tool
    def need_more(query: str, since_days: int = 7) -> dict:
        """Ask the system to load more observations matching a query, then re-run this conversation.

        Args:
            query: words to search for in older observations
            since_days: how far back to look, in days
        """
        intents.append({"intent": "NeedMore", "query": query, "since_days": int(since_days)})
        return {"requested": True}

    return [search_observations, search_memory, remember, schedule_followup, draft_reply, find_contact, connect_service, set_profile, need_more]


@lru_cache
def _model():
    return build_model("chat", temperature=0.2, max_tokens=1024)


def to_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for turn in history:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        if msgs and msgs[-1]["role"] == role:  # Bedrock requires alternating roles
            msgs[-1]["content"][0]["text"] += "\n" + text
        else:
            msgs.append({"role": role, "content": [{"text": text}]})
    if msgs and msgs[0]["role"] == "assistant":
        msgs.pop(0)
    return msgs


def render_user_state(payload: dict[str, Any]) -> str:
    st = payload.get("user_state") or {}
    user = payload.get("user") or {}
    lines = []
    lines.append(f"name: {user.get('display_name') or 'unknown'}")
    lines.append(f"profile set: {'yes' if (user.get('profile') or '').strip() else 'no'}")
    lines.append(f"connected services: {', '.join(st.get('connected') or []) or 'none'}")
    if st.get("pending"):
        lines.append(f"connection in progress: {', '.join(st['pending'])}")
    lines.append(f"available services: {', '.join(st.get('available') or ['gmail', 'googlecalendar'])}")
    if st.get("is_new"):
        lines.append("this is the person's FIRST conversation with you")
    return "\n".join(lines)


def chat(payload: dict[str, Any]) -> dict[str, Any]:
    intents: list[dict[str, Any]] = []
    user = payload.get("user") or {}
    agent = Agent(
        model=_model(),
        system_prompt=CHAT_SYSTEM_PROMPT.format(profile=user_profile(payload), now=payload.get("now_iso", ""),
                                                language=user_language(user.get("language")), user_state=render_user_state(payload)),
        tools=make_tools(payload, intents),
        messages=to_messages(payload.get("history") or []),
        callback_handler=None,
    )
    result = agent(str(payload.get("message") or ""))
    return {"reply": str(result).strip(), "intents": intents}
