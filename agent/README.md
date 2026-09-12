# agent/ — the only package deployed to Bedrock AgentCore Runtime

Entry point: `agent/app.py` (`BedrockAgentCoreApp`, `@app.entrypoint`). Payload router by `task`:

| task       | input                                                                                   | output                                              |
|------------|-----------------------------------------------------------------------------------------|-----------------------------------------------------|
| `triage`   | `{"observation": {source, kind, occurred_at, payload}, "sender_context", "user"}`       | `{"result": TriageResult, model_id, latency_ms}`    |
| `chat_ack` | `{"message", "history", "now_iso", "user"}`                                              | `{"needs_work", "message"}` — the first reflex      |
| `chat`     | `{"message", "history", "recent_observations", "memory_hits", "now_iso", "user", "user_state", …}` | `{"reply", "intents": [...]}`            |
| `learn`    | `{"service", "facts": [metadata sample], "user"}`                                        | `LearnResult`: facts, profile suggestion, top people, message |

Invariants (enforced by tests):
- no `psycopg` / `core.db` / `core.repo` imports here (`tests/phase1/test_agent_contract.py`,
  `tests/phase3/test_chat.py::test_agent_never_touches_db`);
- tools return data or *intents*; workers apply side effects;
- model ids and provider come from env (`TRIAGE_MODEL_ID`, `CHAT_MODEL_ID`, `MODEL_PROVIDER`, `AWS_REGION`,
  `USER_LANGUAGE`); the person's profile comes only from the payload (`user.profile`), never from env.

Workers call it through `agent/client.py::AgentClient` with `AGENT_MODE=local` (in-process) or
`AGENT_MODE=agentcore` (`bedrock-agentcore` `invoke_agent_runtime` with `AGENTCORE_RUNTIME_ARN`).
