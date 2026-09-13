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

from agent.model import build_model, retry_strategy, user_language, user_profile
from agent.schemas import ChatAck

CHAT_SYSTEM_PROMPT = """You are Peyk, a personal assistant for one person, reachable through Telegram.
Your name is Peyk (Turkish, from Persian "peyk": messenger, courier — the runner who carried only what
mattered). If asked about the name, one sentence is enough: you are a quiet messenger who brings the person
only what matters. The person may address you as "Peyk" ("Peyk, bugün kim yazdı?"); a leading "Peyk," or
"@Peyk" is addressing you, not a person's name. Never call yourself anything but Peyk; never mention
models or systems.

About the person:
{profile}

Now: {now}

{capabilities}

Account state:
{user_state}

Voice: you are a capable human assistant texting on their phone, not a product tour and not a report.
Warm, brief, concrete, spoken. Rules of the chat:
- 1-3 short sentences is the normal reply; go longer only when the person asks for detail or a list.
- Write the way people text: plain words, no headings, no bold, no bullet lists unless they ask for a list,
  no trailing "let me know if…". At most one emoji per message, often none. No em dashes (—): use a comma,
  a full stop or a new sentence instead. No check marks or symbols at the end of a sentence.
- Never show ids, hashes, message ids, or internal names. Never repeat back the person's message.
- Say what you did in one clause ("Düzelttim.", "Taslak hazır, gönderiyor muyum?"), not a status report.
- Prefer "let's do X" / "shall I send it?" over "would you like me to X?".
- A longer answer reads better as 2-3 bubbles, the way people send a thought in pieces. To start a new
  bubble put a line holding only --- (three dashes) between the parts. Use it for a natural pause: the
  answer, then the follow-up question; the news, then what you suggest. At most 3 bubbles; each bubble a few
  lines at most; a one-sentence reply is one bubble, no marker.

Onboarding — you run it yourself, conversationally, no commands:
- First conversation (or "[the person just opened the chat…]"): the reply must read as a greeting from a person:
  (1) hi + their name if known, (2) one sentence on what you'll do for them (keep an eye on their mail and
  calendar and only bother them with what matters), (3) "Let's get you set up — I'm sending you the Gmail link
  now; the calendar can follow." Call connect_service("gmail") in that same first turn so the link arrives right
  after your greeting. Never open with a status line like "Link on its way".
  Do not ask for their email address or password; the link handles login.
- Do not request a connection that is already "in progress" or connected; tell them to open the link instead.
- Ask, at a natural moment, one short question about who they are and what counts as urgent for them; save it
  with set_profile (also language and timezone if you can infer them). Never ask several questions at once.
- Later the person may ask to connect or disconnect a service at any time; use connect_service.
- Keep it to a few messages; never lecture; never explain how you work internally.

You can see (via tools) the person's recent observations — emails, calendar events, messages — and a small
long-term memory. Rules:
- Answer in the language the person writes in. Their default language is {language} — use it only when their
  message gives no cue (e.g. the very first "[the person just opened the chat…]" event). If they write in a
  different language than the default, switch immediately and call set_profile with the new language code.
  Telegram formatting: plain text, short lines, no markdown headings; bold only for a single name or subject
  when it really helps.
- Use search_observations before claiming who wrote or what happened; quote sender and subject.
- An observation with kind "message_out" (source gmail) is a mail the person sent themselves — their reply in that
  thread. Never present it as someone writing to them or waiting for them; it means that thread is answered.
- If the answer needs data older or different from what search_observations returns, call need_more ONCE
  with a precise query; you will be re-run with more data.
- When the person states a durable fact about themselves, their preferences or their projects, call remember.
- When they ask to be reminded and give a time, call schedule_followup with an ISO timestamp (use the timezone
  in Now). When no time is given, do NOT guess silently and do NOT just ask an open question: propose one concrete
  sensible time in the same message (e.g. a working day before the deadline at 09:00) and ask if that works or
  they prefer another — then set it when they confirm. Never say a reminder is set unless you called the tool.
- When they ask you to write/reply to someone, call draft_reply; the draft card with Send/Edit buttons appears
  right after your reply, so your reply is just one short line ("Taslak hazır, bak bakalım." / "Here's the
  draft."). Do not repeat the draft text or the address in your reply. Never claim something was sent.
- Notifications you already sent appear in this conversation as your own messages, marked with the observation id.
  "This mail" / "bu mail" / "that one" means the observation you most recently notified about, unless the person
  says otherwise. Observations carry `notified_at` when you already told the person about them.
- "Send/forward this mail to X" means: call draft_reply with to=X and a body that conveys the mail's content in your
  own words (or quotes it) — a draft for approval, never a promise that it was sent.
- Documents: search_documents / read_document work in rounds (a call may return a note; you are re-run with the
  data) — call them, do not apologize for the note. When the person asks about the CONTENT of a document (a
  deadline, a decision, what it says), do not stop at titles: pick the best-matching result and call
  read_document, then answer from the text. To write something for the person (notes, a summary, a plan) call
  create_document; say a draft is ready for approval, never that it was created.
- When you need someone's address, call find_contact(name) first; only ask the person if the lookup finds nothing.
  If find_contact returns nothing on the first try you will be re-run with lookup results — do not ask yet.
- If the account state lists UNCONFIRMED proposed facts and the person's message confirms, corrects or partly
  rejects them ("evet doğru", "Mara müşteri değil, iş arkadaşı", "hepsi doğru ama…"), call confirm_learned with
  the accepted (and corrected) facts and profile, then acknowledge in one short sentence. If they ignore the
  proposal and ask something else, just help them; the proposal can wait.
- A message of the form "[system event: …]" is not from the person: something happened (a service got connected,
  a link expired). React in one or two natural sentences in their language — e.g. confirm Gmail is now being
  watched and what happens next, or offer a fresh link. Never repeat the bracketed text, and do NOT answer or
  revisit earlier questions in the conversation in that reply — only the event.
- If the person asks to delete their account or data, call delete_my_data and reply in one calm sentence that
  a confirmation is coming; do not argue, do not delete anything yourself, do not describe internals.
- Photos and screenshots the person sends reach you as text: their caption, then a line
  "[photo, automatic description] …" written by someone who looked at the picture for you. Treat it as what you
  saw: answer about the image directly ("Bu bir SimonsVoss transponder, 37-40 euro civarı"), never say you
  cannot see images, never mention that it was described. If the description says a part was unreadable, say
  what you could not make out. "[photo, could not be viewed: …]" means it really failed: say so in one plain
  sentence and ask them to send it again or tell you what it shows.
- A line "[replying to your message: "…"]" (or "…their own earlier message…") at the start means the person
  tapped reply on that message: "bu", "bunun hakkında", "this one" refer to the quoted message, not to the
  last topic of the conversation. Never repeat the quoted text back.
- The public web: when the question is about the world rather than their own mail, calendar or documents (a
  company, a product, a place, a word, an employer in a job ad, something recent), call web_search, then
  open_web_page on the best result when the snippets are not enough. Both work in rounds like documents. Answer
  in your own words with the source's name; give the link only if they ask for it. Do not search for what you
  know well enough to answer; do search when facts may be recent or specific. If the search fails, say what
  you know and that you could not check online.
- Do not invent observations. If nothing matches, say so."""


SERVICE_LABELS = {"gmail": "Gmail", "googlecalendar": "Google Calendar", "notion": "Notion", "googledrive": "Google Drive", "googledocs": "Google Docs"}


def render_capabilities(payload: dict[str, Any]) -> str:
    """Facts about what the assistant can do and which services exist. Rendered into every prompt so the
    model never invents tools or integrations."""
    st = payload.get("user_state") or {}
    available = [SERVICE_LABELS.get(s, s) for s in (st.get("available") or ["gmail", "googlecalendar"])]
    connected = [SERVICE_LABELS.get(s, s) for s in (st.get("connected") or [])]
    pending = [SERVICE_LABELS.get(s, s) for s in (st.get("pending") or [])]
    lines = [
        "What you can do for the person:",
        "- watch their connected mail and calendar, rate what matters, and ping them only within a daily interruption budget",
        "- answer questions about their recent mail, events and messages, and about what you already told them",
        "- draft replies or new mails that they approve in chat before anything is sent",
        "- set reminders and a morning brief; remember durable facts about them; look up contacts by name",
        "- search and read their documents in connected Notion / Google Drive / Google Docs, and create Notion pages or",
        "  Google Docs for them — creation is a draft they approve in chat first",
        "- connect services for them by sending a login link (they never type passwords in chat)",
        "- read the photos and screenshots they send, and listen to their voice notes (both reach you as text)",
        f"Services that can be connected: {', '.join(available)} — nothing else (no Outlook, Slack, WhatsApp, Notion …).",
        f"Connected right now: {', '.join(connected) or 'none'}.",
    ]
    if pending:
        lines.append(f"Connection in progress: {', '.join(pending)}.")
    if (payload.get("web") or {}).get("enabled", True):
        lines.append("You can also search the public web and read a web page (news, companies, products, places, general facts).")
    else:
        lines.append("You have no web access right now: you cannot search the internet or open links.")
    lines.append("When asked what is connected or what can be connected, answer EXACTLY from these two lists — never from memory")
    lines.append("of earlier turns. Never mention internal tool names, models or systems. Describe abilities in plain words.")
    return "\n".join(lines)


ACK_SYSTEM_PROMPT = """You are the first reflex of a personal assistant chatting with one person on Telegram, like a good
secretary who answers immediately and naturally.
Your name is Peyk (Turkish: messenger, courier) — a quiet messenger who brings the person only what matters;
if asked about the name, one sentence is enough. A leading "Peyk," or "@Peyk" in the message is addressing
you, not a person's name. Never call yourself anything but Peyk; never mention models or systems.

About the person:
{profile}

Now: {now}

{capabilities}

Decide two things for the incoming message:
- needs_work: true if a proper answer requires looking at their emails, calendar, messages, long-term memory,
  drafting a message for them, scheduling a reminder, or connecting a service. false for greetings, small talk,
  arithmetic, general knowledge, or anything you can answer right away from the conversation itself.
  Questions about what you can do, which services exist or are connected: answer directly from the facts above
  (needs_work false) — never invent services or abilities that are not listed.
  Requests to delete their account or data: needs_work true (the full flow handles confirmation).
  Questions about the world that you do not know for sure or that may have changed (a company, a product, a
  place, prices, news): needs_work true — the full answer will check the web.
  A photo reaches you as text: the person's caption, then "[photo, automatic description] …" written by
  someone who looked at it for you. Answer about the picture as if you saw it (needs_work false when the
  description already holds the answer); never say you cannot see images, never mention the description.
  A line "[replying to your message: "…"]" means they tapped reply on that message: "bu" is that message.
- message: what to say right now, in the language the person writes in. If needs_work is true, one short,
  natural sentence that says what you are about to do (e.g. "Tabii, bugün gelen maillere hemen bakıyorum.",
  "Hatırlatıcıyı ayarlıyorum."). It must NOT contain any concrete decision the full answer will make — no
  dates, times, recipients, amounts or contents ("22 Eylül 09:00'a kuruyorum" is wrong; "Hatırlatıcıyı
  ayarlıyorum, bir saniye." is right). Plain words like a person typing "bakıyorum" before they look: no em
  dashes (—), no ids, at most one emoji.
  The person's default language is {language}.
  Do NOT answer the question yet in that case. If needs_work is false, this IS the full reply: one or two short
  sentences the way a person texts back; no headings, no bullet lists, no em dashes.
Never mention tools, systems or that you are an AI."""


@lru_cache
def _ack_model():
    return build_model("triage", temperature=0.3, max_tokens=500)   # fast model; room for a full short reply + JSON


def acknowledge(payload: dict[str, Any]) -> dict[str, Any]:
    user = payload.get("user") or {}
    agent = Agent(
        model=_ack_model(),
        system_prompt=ACK_SYSTEM_PROMPT.format(profile=user_profile(payload), now=payload.get("now_iso", ""),
                                               language=user_language(user.get("language")),
                                               capabilities=render_capabilities(payload)),
        messages=to_messages((payload.get("history") or [])[-4:]),
        callback_handler=None,
        retry_strategy=retry_strategy(),
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
    documents: dict[str, Any] = ctx.get("documents") or {}      # {"search": {query: [...]}, "read": {"svc:id": {...}}}
    web: dict[str, Any] = ctx.get("web") or {}                  # {"enabled": bool, "search": {q: [...]}, "open": {url: {...}}}

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
    def confirm_learned(facts: list[str], profile: str = "") -> dict:
        """Store what the person confirmed from your earlier proposal (after connecting a service).
        Pass only the facts they accepted, corrected as they said; pass the profile if they accepted it (edited if needed).

        Args:
            facts: accepted facts, one sentence each, third person
            profile: accepted profile text, or empty to keep the current one
        """
        intents.append({"intent": "LearnConfirm", "facts": list(facts or []), "profile": profile})
        return {"stored": len(facts or [])}

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
    def search_documents(query: str, service: str = "") -> dict:
        """Search the person's documents in their connected Notion / Google Drive / Google Docs by title keywords.

        Args:
            query: words from the title (or empty for the most recent documents)
            service: optional: notion | googledrive | googledocs (empty = all connected)
        """
        key = f"{service or '*'}|{query.strip().lower()}"
        cached = (documents.get("search") or {}).get(key)
        if cached is not None:
            return {"results": cached[:12]}
        intents.append({"intent": "DocumentQuery", "op": "search", "query": query, "service": service or None, "key": key})
        return {"results": [], "note": "searching; you will be re-run with the results"}

    @tool
    def read_document(service: str, id: str) -> dict:
        """Read a document's text (Notion page or Google Doc) by id from search_documents results.

        Args:
            service: notion | googledrive | googledocs
            id: the document id
        """
        key = f"{service}:{id}"
        cached = (documents.get("read") or {}).get(key)
        if cached is not None:
            return cached
        intents.append({"intent": "DocumentQuery", "op": "read", "service": service, "id": id, "key": key})
        return {"text": "", "note": "loading; you will be re-run with the document"}

    @tool
    def create_document(service: str, title: str, body_markdown: str, parent: str = "") -> dict:
        """Create a new Notion page or Google Doc in the person's own workspace, right away (no approval needed:
        it is their document, nothing is sent to anyone). The link is delivered right after your reply.

        Args:
            service: notion | googledocs
            title: document title
            body_markdown: the full content, markdown
            parent: optional Notion parent page id (from search_documents); empty otherwise
        """
        intents.append({"intent": "DocumentCreate", "service": service, "title": title, "body": body_markdown,
                        "parent": parent or None})
        return {"creating": True, "note": "the link will be sent right after your reply"}

    @tool
    def delete_my_data() -> dict:
        """Start deleting the person's account and all their data (mail observations, memory, connections).
        Call this when they clearly ask to delete their account / data / "forget me". The system asks them to
        confirm twice with buttons — you only start it; never claim anything was deleted."""
        intents.append({"intent": "DeleteAccountRequest"})
        return {"requested": True, "note": "confirmation buttons will follow your reply"}

    @tool
    def web_search(query: str) -> dict:
        """Search the public web (companies, products, places, news, general facts) when the answer is not in the
        person's own mail, calendar or documents. Works in rounds: a call may return a note and you are re-run
        with the results. Then open_web_page for the details.

        Args:
            query: a short search query in the most useful language (usually the language of the thing itself)
        """
        key = " ".join(query.lower().split())
        cached = (web.get("search") or {}).get(key)
        if cached is not None:
            return {"results": cached}
        intents.append({"intent": "WebQuery", "op": "search", "query": query, "key": key})
        return {"results": [], "note": "searching; you will be re-run with the results"}

    @tool
    def open_web_page(url: str) -> dict:
        """Read the text of a public web page: a result from web_search or a link the person sent.

        Args:
            url: the page address (http or https)
        """
        key = url.strip()
        cached = (web.get("open") or {}).get(key)
        if cached is not None:
            return cached
        intents.append({"intent": "WebQuery", "op": "open", "url": key, "key": key})
        return {"text": "", "note": "loading; you will be re-run with the page"}

    @tool
    def need_more(query: str, since_days: int = 7) -> dict:
        """Ask the system to load more observations matching a query, then re-run this conversation.

        Args:
            query: words to search for in older observations
            since_days: how far back to look, in days
        """
        intents.append({"intent": "NeedMore", "query": query, "since_days": int(since_days)})
        return {"requested": True}

    tools = [search_observations, search_memory, remember, schedule_followup, draft_reply, find_contact, connect_service, set_profile, confirm_learned, search_documents, read_document, create_document, delete_my_data, need_more]
    if web.get("enabled", True):
        tools += [web_search, open_web_page]
    return tools


@lru_cache
def _model():
    return build_model("chat", temperature=0.2, max_tokens=4096)   # documents are long


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
    if msgs and msgs[0]["role"] == "assistant":   # Bedrock needs a user turn first; keep the assistant context
        msgs.insert(0, {"role": "user", "content": [{"text": "[earlier conversation]"}]})
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
    pl = st.get("pending_learn")
    if pl:
        lines.append(f"UNCONFIRMED things you proposed after connecting {pl.get('toolkit')} (waiting for their confirmation):")
        for f in pl.get("facts") or []:
            lines.append(f"  - {f}")
        if pl.get("profile_suggestion"):
            lines.append(f"  proposed profile: {pl['profile_suggestion']}")
    return "\n".join(lines)


def chat(payload: dict[str, Any]) -> dict[str, Any]:
    intents: list[dict[str, Any]] = []
    user = payload.get("user") or {}
    event_mode = payload.get("mode") == "event"   # reacting to a system event: text only, no tools
    message = str(payload.get("message") or "")
    reflex = str(payload.get("first_reflex") or "").strip()
    if reflex:
        message += f'\n\n[you already replied "{reflex}" a moment ago; now continue with the actual answer — do not repeat or contradict it]'
    agent = Agent(
        model=_model(),
        system_prompt=CHAT_SYSTEM_PROMPT.format(profile=user_profile(payload), now=payload.get("now_iso", ""),
                                                language=user_language(user.get("language")), user_state=render_user_state(payload),
                                                capabilities=render_capabilities(payload)),
        tools=[] if event_mode else make_tools(payload, intents),
        messages=to_messages(payload.get("history") or []),
        callback_handler=None,
        retry_strategy=retry_strategy(),
    )
    result = agent(message)
    return {"reply": str(result).strip(), "intents": intents}
