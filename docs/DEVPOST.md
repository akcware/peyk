# Devpost submission text (draft)

**Project name:** Proactive Agent  
**Track:** Everyday Agents  
**One-liner:** A personal agent that watches your inbox and calendar, interrupts you only when it matters, and never sends anything you didn't approve.

## Inspiration
Notifications treat a newsletter and a client's "production is down" the same way. We wanted an assistant that behaves like a good secretary: reads everything, brings you the three things that matter, drafts the reply, and waits for your nod.

## What it does
- Observes Gmail and Google Calendar (via Composio triggers) and your Telegram chat as one stream of observations.
- Triages each one with Claude Haiku 4.5 (urgency 1–5, category, a one-line summary in your language).
- Applies an interruption budget — daily quota, per-thread cooldown, quiet hours, mutes, with a reserve for emergencies — as a pure, testable function. 60 mails a day become 3–4 pings.
- Chats like a secretary (Claude Sonnet 4.6): a natural first reflex, then the answer from real data; remembers what it told you; finds contacts itself.
- Drafts replies you approve in chat. Edits change a content hash; an old Send button can never send an edited draft.
- Morning brief, reminders, and onboarding by conversation: say hi, answer one question, tap the login link.
- Multi-user from day one; adding a source is a data change (Calendar: one mapping row, core untouched — enforced by a test).

## How we built it
Python 3.12, Strands Agents SDK on Amazon Bedrock (Haiku 4.5 triage, Sonnet 4.6 chat, Titan Embeddings v2), Composio for OAuth/triggers/actions, Postgres 16 + pgvector as both database and queue (`FOR UPDATE SKIP LOCKED`), FastAPI webhook gateway, Telegram Bot API over httpx. The `agent/` package never touches the database, so it deploys to Bedrock AgentCore Runtime (CodeZip config included). 106 tests, phase gates with git tags.

## Challenges
Composio triggers are polling + push without replay: we measured a 49 s median latency and that a disabled trigger loses events, so a reconcile job backfills every 10 minutes. A replay of 60 labeled mails showed morning urgency-3/4 pings starving an afternoon emergency — hence the emergency reserve in the budget. Keeping the model out of the "should I interrupt?" decision was the key design call.

## What's next
WhatsApp as a second control channel (the control layer is already channel-agnostic), Slack as a source, and per-person learning from 👍/👎 feedback.
