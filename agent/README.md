# agent/ — the only package deployed to Bedrock AgentCore Runtime

Entry point: `agent/app.py` (`BedrockAgentCoreApp`, `@app.entrypoint`). Payload router:

| task     | input                                                                 | output                                  |
|----------|-----------------------------------------------------------------------|-----------------------------------------|
| `triage` | `{"observation": {source, kind, occurred_at, payload}, "sender_context"}` | `{"result": TriageResult, model_id, latency_ms}` |
| `chat`   | `{"message", "history", "recent_observations", "memory_hits", "now_iso"}` | `{"reply", "intents": [...]}`           |

Invariants (enforced by tests):
- no `psycopg` / `core.db` / `core.repo` imports here (`tests/phase3/test_chat.py::test_agent_never_touches_db`);
- tools return data or *intents*; workers apply side effects;
- model ids and provider come from env (`TRIAGE_MODEL_ID`, `CHAT_MODEL_ID`, `MODEL_PROVIDER`, `AWS_REGION`, `USER_PROFILE`).

Workers call it through `agent/client.py::AgentClient` with `AGENT_MODE=local` (in-process) or
`AGENT_MODE=agentcore` (`bedrock-agentcore` `invoke_agent_runtime` with `AGENTCORE_RUNTIME_ARN`).
