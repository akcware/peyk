# Devpost submission text (draft)

**Project name:** Peyk  
**Track:** Everyday Agents  
**One-liner:** Peyk (Turkish: messenger, courier) is a personal agent that watches your inbox and calendar, interrupts you only when it matters, and never sends anything you didn't approve.

## Inspiration
Notifications treat a newsletter and a client's "production is down" the same way. We wanted an assistant that behaves like a good secretary: reads everything, brings you the three things that matter, drafts the reply, and waits for your nod.

## What it does
- Observes Gmail and Google Calendar (via Composio triggers) and your Telegram chat as one stream of observations.
- Triages each one with Claude Haiku 4.5 (urgency 1–5, category, a one-line summary in your language).
- Applies an interruption budget — daily quota, per-thread cooldown, quiet hours, mutes, with a reserve for emergencies — as a pure, testable function. 60 mails a day become 3–4 pings.
- Chats like a secretary (Claude Sonnet 4.6): a natural first reflex, then the answer from real data; remembers what it told you; finds contacts itself.
- Drafts replies you approve in chat. Edits change a content hash; an old Send button can never send an edited draft.
- Morning brief, reminders, and onboarding by conversation: say hi, answer one question, tap the login link.
- Multi-user from day one; onboarding is a conversation (say hi, answer one question, tap the login link — no commands).
- Gets to know you with consent: after a service connects it reads only metadata, proposes a few facts and a profile, and stores nothing until you confirm.
- Finds and reads your Notion / Google Docs / Drive documents, writes pages and docs for you — as drafts you approve.
- "Delete my account" asks twice, then revokes the service connections and erases everything.
- Adding a source is a data change (Calendar: one mapping row, core untouched — enforced by a test).

## How we built it
Python 3.12, Strands Agents SDK on Amazon Bedrock (Claude Haiku 4.5 for triage, Claude Sonnet 4.6 for chat, Titan Embeddings v2), Composio for OAuth/triggers/actions (Gmail, Google Calendar, Notion, Google Drive, Google Docs), Postgres + pgvector as both database and queue (`FOR UPDATE SKIP LOCKED`), Telegram Bot API over httpx. The `agent/` package never touches the database, so it can run isolated on Bedrock AgentCore Runtime (CodeZip config included; our hackathon account's organization policy blocks AgentCore/compute, so the worker runs on Fly.io with Neon Postgres while the models stay on Bedrock). 120 tests, phase gates with git tags. Live: https://akcware.github.io/peyk/ · https://t.me/proactiveagent_bot

## Challenges
Composio triggers are polling + push without replay: we measured a 49 s median latency and that a disabled trigger loses events, so a reconcile job backfills every 10 minutes. A replay of 60 labeled mails showed morning urgency-3/4 pings starving an afternoon emergency — hence the emergency reserve in the budget. Keeping the model out of the "should I interrupt?" decision was the key design call.

## What's next
WhatsApp as a second control channel (the control layer is already channel-agnostic), Slack and Outlook as sources, AgentCore deployment on an unrestricted account, and per-person learning from 👍/👎 feedback.
