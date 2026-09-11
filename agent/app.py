"""AgentCore Runtime entrypoint. Payload router: {"task": "triage", ...} | {"task": "chat", ...}.

The agent never touches the database: it receives everything it needs in the payload and returns
plain JSON. `handle()` is pure Python so AGENT_MODE=local can call it in-process; the
BedrockAgentCoreApp wrapper is only created when the SDK is installed (agentcore dependency group)."""
from __future__ import annotations

import json
import pathlib
import sys
import types
from collections.abc import Callable
from typing import Any

# AgentCore CodeZip ships this directory flat (app.py at the zip root). Register it as package `agent`
# so the absolute imports below work both in the repo (agent/ is a real package) and on the runtime.
_here = pathlib.Path(__file__).resolve().parent
if "agent" not in sys.modules and _here.name != "agent":
    _pkg = types.ModuleType("agent")
    _pkg.__path__ = [str(_here)]
    sys.modules["agent"] = _pkg
elif "agent" not in sys.modules and str(_here.parent) not in sys.path:
    sys.path.insert(0, str(_here.parent))


def handle(payload: dict[str, Any], *, triage_fn: Callable[..., Any] | None = None,
           chat_fn: Callable[..., Any] | None = None, ack_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    task = payload.get("task")
    if task == "chat_ack":
        from agent.chat_agent import acknowledge as _ack

        out = (ack_fn or _ack)(payload)
        return {"task": "chat_ack", "needs_work": bool(out["needs_work"]), "message": out["message"]}
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
