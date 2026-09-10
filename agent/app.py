"""AgentCore Runtime entrypoint. Payload router: {"task": "triage", ...} | {"task": "chat", ...}.

The agent never touches the database: it receives everything it needs in the payload and returns
plain JSON. `handle()` is pure Python so AGENT_MODE=local can call it in-process; the
BedrockAgentCoreApp wrapper is only created when the SDK is installed (agentcore dependency group)."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def handle(payload: dict[str, Any], *, triage_fn: Callable[..., Any] | None = None,
           chat_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    task = payload.get("task")
    if task == "triage":
        from agent.triage_agent import triage as _triage

        fn = triage_fn or _triage
        result, meta = fn(payload.get("observation") or {}, payload.get("sender_context"))
        return {"task": "triage", "result": result.model_dump(), **meta}
    if task == "chat":
        from agent.chat_agent import chat as _chat

        out = (chat_fn or _chat)(payload)
        return {"task": "chat", "reply": out["reply"], "intents": out.get("intents", [])}
    return {"error": f"unknown task {task!r}"}


try:  # pragma: no cover - only on AgentCore
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
        return handle(payload)

    if __name__ == "__main__":
        app.run()
except ImportError:  # local dev / tests without the AgentCore SDK
    app = None

    if __name__ == "__main__":
        import sys

        print(json.dumps(handle(json.loads(sys.stdin.read())), default=str))
