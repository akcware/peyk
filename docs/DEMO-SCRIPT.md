# Demo video script (≤ 5 min)

Screen: Telegram on the left (phone or desktop), a terminal with the worker logs on the right. JSON logs look
good on camera; keep `LOG_LEVEL=INFO`.

Preparation: production (Fly) is running and the local worker is **stopped**. Today's notification quota is
clean (if needed, delete today's rows from `sent_notification`). Put a "Client standup" event in the calendar
18 minutes from the start. Have a friend ready with a second Telegram account. Have a Notion page with a
deadline in it.

## 0:00–0:40 Problem
[Screen: a real Gmail inbox with 60+ unread, or the 60 → 4 animation on the landing page]
"Sixty things land in my inbox every day, and every one of them screams equally. Newsletters, receipts, and the
one mail from a client whose checkout is down. Notifications don't know the difference. So I built an agent that
does."

## 0:40–1:10 What it is
[Screen: landing page hero]
"Peyk — it means messenger in Turkish — watches your mail, calendar and documents, decides what is actually
urgent, and interrupts you only within a daily budget. It talks like a secretary, drafts replies you approve, and
never sends anything on its own. Built with the Strands Agents SDK on Amazon Bedrock."

## 1:10–2:10 Live: triage and the budget
[Send yourself two mails: one "Weekly digest"-style, one "Invoice #2041 due Friday — please confirm today"]
"Two mails just arrived. Watch the worker: both are triaged by Haiku in about two seconds. The newsletter gets
urgency 1 — silent. The invoice gets 4 — and here it is in Telegram, summarised in my language, with useful /
noise / mute buttons."
[Show the log lines `triage.decided … notify=false below_threshold` and `notify=true`]
"The model decides importance. A pure function decides whether it's worth interrupting me: quota, cooldown,
quiet hours, and a reserve kept for real emergencies."

## 2:10–3:10 Live: secretary chat and draft approval
[Telegram: "who mailed me today?"]
"Ask it something. First reflex: 'Sure, checking today's mail…' — then the answer, from real observations."
[Telegram: "write to Mara that we move the meeting to tomorrow" → draft → Edit → "make it 3 pm" → new draft →
tap Send on the *old* message → refused → tap Send on the new one → ✅]
"It drafts, I edit, and here's the part I care about: the old Send button carries the old content hash. It
can't send the edited draft. Only the newest version goes out — to Gmail, through Composio."

## 3:10–3:50 Live: onboarding by conversation, documents
[Friend's phone: Start → Peyk introduces itself, sends the login link → tap → Peyk says "connected" in its own
voice → a minute later "here is what I noticed, is this right?" → "yes"]
"Anyone can use it. No commands: it introduces itself, sends the login link, and once connected it gets to know
you — from metadata only, and it stores nothing until you confirm."
[Your phone: "when is the deadline in my Notion page about X?" → answer → "remind me" → it proposes a time →
"ok". Then: "make me a Google Doc with the agenda for that meeting" → the link arrives]
"It reads my Notion, finds the deadline, proposes a reminder time and sets it only when I agree. Documents in
my own workspace it just creates and hands me the link — they reach nobody else."

## 3:50–4:30 Architecture
[docs/ARCHITECTURE.md diagram]
"One queue in Postgres, adapters for sources, a deterministic gate, and an agent package that never touches the
database — so it can run isolated on Bedrock AgentCore. Composio does auth and triggers; a reconcile job catches
what polling misses, because we measured that a disabled trigger loses events."

## 4:30–5:00 Close
"Sixty a day down to four. Drafts you approve. An assistant that behaves like a good secretary, not a firehose.
Code is MIT on GitHub."
[Screen: repo and https://peyk.chat]
