# Peyk

**Peyk** (Turkish, from Persian *peyk*: messenger, courier — the runner who carried only what mattered) is a
personal agent that watches everything that comes at you — mail, calendar, documents, messages — interrupts you only when it matters, and never sends anything you didn't approve. You talk to it on Telegram, like a good secretary.

- Product page: **https://peyk.chat** (mirror: https://akcware.github.io/peyk/)
- Try it: **https://t.me/proactiveagent_bot** — say hi, it takes it from there.
- Built for the AWS [Agents for Humans](https://agentsforhumans.devpost.com/) hackathon with the Strands Agents SDK on Amazon Bedrock.

## Why not just a chatbot?

A generic assistant waits for you to ask. Your inbox does not wait: sixty things a day, all with the same red badge,
and the one that matters — a client whose checkout is down, the landlord, the school nurse — is buried between a
newsletter and a receipt. Peyk starts from the other end.

| Generic AI assistant | Peyk |
|---|---|
| Answers when asked | **Watches** your mail, calendar and documents and comes to you |
| Every notification looks the same | Rates urgency, then a **daily interruption budget** decides whether to knock at all — the rest waits for the morning brief |
| Talks about your data from a paste | Reads your **real** observations; remembers what it already told you ("that mail" works) |
| "Send" means gone | Every outgoing mail, reminder or document is a **draft you approve**; an edited draft can never be sent by an old button |
| Knows nothing about you, or everything | Asks once who you are, learns from **metadata only**, and stores **nothing until you confirm** |
| One user, one API key | Anyone can message it; onboarding is a **conversation**, not commands |

## What it does

**Interrupts on a budget.** Each mail or event gets an urgency 1–5 and a one-line summary in your language. A
deterministic gate then applies your budget: a daily quota (default 10), a per-thread cooldown, quiet hours,
mutes — with slots reserved for real emergencies so a busy morning cannot starve an afternoon crisis. Sixty
mails a day become a handful of knocks; everything else lands in the 08:00 brief.

**Talks like a secretary.** Ask "who wrote today?", "when is the deadline in my Notion page?", "did I answer
Mara?". Peyk answers first with a natural reflex ("Sure, checking today's mail…"), then with facts from your
data. It looks up contacts itself, reads the document instead of guessing from its title, and proposes a time
before it sets a reminder.

**Drafts, you decide.** "Write to Mara that we move the meeting to tomorrow" → a draft with ✅ Send · ✏️ Edit ·
❌ Cancel. Edit changes the draft's content hash; the Send button of an older version is refused. Documents in
your own workspace (Notion pages, Google Docs) are created directly and you get the link — they reach nobody else.

**Gets to know you — with consent.** Right after you connect a service Peyk reads the last 30 days of
*metadata* (senders, subjects, document titles, never bodies), then says what it noticed and asks you to confirm
or correct. Only confirmed facts go into memory; sensitive situations are asked about, never inferred.

**Yours to delete.** Say "delete my account": Peyk asks twice, then revokes the service connections and erases
everything it holds about you.

## Works with

| Service | What Peyk does with it |
|---|---|
| Gmail | watches new mail, summarizes, drafts replies and new mails for approval |
| Google Calendar | knows your day, tells you when someone invites you, moves or cancels a meeting or answers your invite (once, even when Google also mails it), pings you 15 minutes before an event, puts today's events in the morning brief, and adds, moves or cancels events when you ask (anything that tells other people waits for your tap) |
| Notion, Google Docs, Google Drive | finds and reads your documents; writes pages and docs for you and sends the link |
| Telegram | where you talk to Peyk (WhatsApp next — the control channel is an adapter) |

Auth and triggers go through [Composio](https://composio.dev); Peyk never sees your Google or Notion password.

## Under the hood (short version)

- **Strands Agents SDK** on **Amazon Bedrock**: Claude Haiku 4.5 rates and summarizes (structured output),
  Claude Sonnet 4.6 chats with tools, Titan Embeddings v2 backs the memory. The agent package never touches the
  database: tools return data or *intents*, workers apply side effects — which is what lets it run isolated on
  Bedrock AgentCore Runtime (configuration in `agentcore/`).
- **Postgres + pgvector** is both the database and the queue (`FOR UPDATE SKIP LOCKED`); every event from every
  source is one observation row, deduplicated by its natural id, so replays and reconciles are safe.
- **Adding a source is a data change**: Google Calendar was added with one mapping row and no change to the core
  — a test enforces it.
- Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · deployment: [docs/DEPLOY.md](docs/DEPLOY.md) ·
  test record: [docs/ENGINEERING.md](docs/ENGINEERING.md).

## Run it yourself

```bash
cp .env.example .env        # Telegram bot token, Composio API key, Bedrock model ids (see docs/SETUP.md)
uv sync --group dev
make migrate                # local Postgres via docker compose
make run-workers            # then message your bot on Telegram
make test                   # 120+ tests, no model calls
```

Production runs as one container (`make deploy` → Fly.io) with Neon Postgres and Bedrock; see
[docs/DEPLOY.md](docs/DEPLOY.md).

## License

MIT
