# Architecture

Peyk is built on one idea: **observe everything, interrupt rarely, act only with approval.** Every input (mail, calendar
event, Telegram message, scheduler tick) becomes an `observation` row; one worker loop consumes the queue;
the model decides *importance*, a deterministic gate decides *interruption*; every outbound action is a
hash-checked draft the user approves in Telegram.

```mermaid
flowchart LR
    subgraph Sources
        GM[Gmail]:::ext
        GC[Google Calendar]:::ext
        TG[Telegram]:::ext
    end
    subgraph Composio["Composio (auth + triggers + actions)"]
        TR[triggers<br/>GMAIL_NEW_GMAIL_MESSAGE<br/>GOOGLECALENDAR_EVENT_STARTING_SOON]
        AC[actions<br/>FETCH_EMAILS · SEND · REPLY]
    end
    GM --> TR
    GC --> TR
    TR -- websocket (local) --> ING
    TR -- webhook (deploy) --> GW[gateway<br/>FastAPI /webhook/composio<br/>HMAC verify → insert]
    TG -- getUpdates --> ING

    subgraph Workers["workers (long-lived, Docker)"]
        ING[ingest<br/>adapter.subscribe → insert<br/>ON CONFLICT DO NOTHING]
        SCH[scheduler<br/>due jobs → tick observations]
        TRI[triage loop<br/>claim_next FOR UPDATE SKIP LOCKED]
        GATE[gate.decide<br/>pure: quota · cooldown · quiet · mutes · bypass reserve]
        CHAT[chat + intents]
        APPR[approval<br/>draft → approve(hash) → send]
    end
    subgraph DB["Postgres 16 + pgvector"]
        OBS[(observation<br/>= queue)]
        TRG[(triage · sent_notification<br/>mute_rule · budget_settings)]
        JOB[(scheduled_job)]
        MEM[(memory<br/>vector 1024)]
        ACT[(action<br/>content_hash · approved_hash)]
    end
    GW --> OBS
    ING --> OBS
    SCH --> JOB
    SCH --> OBS
    OBS --> TRI
    TRI --> AGENT
    TRI --> GATE --> TGOUT[Telegram sendMessage<br/>👍 👎 🔇]
    TRI --> TRG
    TRI --> CHAT --> AGENT
    CHAT --> MEM
    CHAT --> JOB
    CHAT --> APPR --> ACT
    APPR -- approved + hash match --> AC
    AC --> GM

    subgraph AgentCore["Bedrock AgentCore Runtime — agent/ only, no DB access"]
        AGENT[app.py router<br/>task=triage → Haiku 4.5 structured output<br/>task=chat → Sonnet 5 + tools → intents]
    end
    AGENT -. Bedrock .-> BR[(Claude on Bedrock<br/>Titan Embeddings v2)]

    classDef ext fill:#eee,stroke:#999,color:#333
```

## Decisions that shaped the code

| Decision | Why | Where |
|---|---|---|
| Postgres is the queue | one system, `FOR UPDATE SKIP LOCKED`, crash recovery is an `UPDATE` | `core/queue.py` |
| Dedup in the DB, not in adapters | `unique (user_id, source, source_key)`; every path (ws, webhook, backfill, reconcile) is safe to replay | `db/migrations/0001_core.sql` |
| Importance vs. interruption are different questions | the model rates urgency; a pure function with a daily budget decides whether to ping | `agent/triage_agent.py`, `workers/gate.py` |
| Budget keeps a reserve for urgency 5 | replay showed morning 3/4s starving an afternoon emergency | `bypass_reserve` in `workers/gate.py` |
| Scheduler emits ticks, does no work | one consumer loop, one failure model; reminders, briefs and reconcile are just observations | `workers/scheduler.py`, `workers/ticks.py` |
| `reconcile` every 10 min | Composio triggers are polling + push without replay; a disabled trigger loses events (measured) | `workers/ticks.py::reconcile` |
| Agent never touches the DB | required for AgentCore isolation; tools return data or intents, workers apply side effects | `agent/`, `tests/phase3::test_agent_never_touches_db` |
| Approval carries the content hash | an old Telegram button can never send an edited draft | `workers/actions.py` |
| Adding a source = one mapping row | phase 5 gate: `git diff phase-4..HEAD` must not touch core/workers/gateway/agent | `adapters/composio/mappings.py`, `tests/phase5` |

## Processes

| Process | Runs | Talks to |
|---|---|---|
| `gateway` | FastAPI | Postgres (insert only) |
| `workers` | asyncio tasks: ingest per adapter, scheduler, triage/chat/approval loop, maintenance | Postgres, Telegram, Composio actions, AgentCore (or in-process agent) |
| `agent` | AgentCore Runtime (CodeZip, `agentcore deploy`) | Bedrock only |
| `sessions` | placeholder for stateful adapters (WhatsApp, cut from the hackathon scope) | Postgres |
