"""AgentClient: workers call this; it either runs agent/app.py in-process (local) or invokes the
AgentCore runtime (agentcore). Same payload contract both ways."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

from agent.schemas import TriageResult


class AgentClient:
    def __init__(self, mode: str | None = None, *, runtime_arn: str | None = None, region: str | None = None,
                 handle_fn: Any | None = None) -> None:
        self.mode = mode or os.environ.get("AGENT_MODE", "local")
        self.runtime_arn = runtime_arn or os.environ.get("AGENTCORE_RUNTIME_ARN", "")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._handle = handle_fn
        self._boto = None

    async def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.mode == "agentcore":
            return await asyncio.to_thread(self._invoke_agentcore, payload)
        if self._handle is None:
            from agent.app import handle

            self._handle = handle
        return await asyncio.to_thread(self._handle, payload)

    def _invoke_agentcore(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.runtime_arn:
            raise RuntimeError("AGENTCORE_RUNTIME_ARN not set")
        if self._boto is None:
            import boto3

            self._boto = boto3.client("bedrock-agentcore", region_name=self.region)
        resp = self._boto.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=payload.get("session_id") or uuid.uuid4().hex + "-proactive-agent-session",
            payload=json.dumps(payload, default=str).encode(),
        )
        body = resp["response"].read() if hasattr(resp.get("response"), "read") else resp.get("response", b"")
        return json.loads(body)

    async def triage(self, observation: dict[str, Any], sender_context: dict[str, Any] | None) -> tuple[TriageResult, dict[str, Any]]:
        out = await self.invoke({"task": "triage", "observation": observation, "sender_context": sender_context})
        if "error" in out:
            raise RuntimeError(f"agent error: {out['error']}")
        return TriageResult(**out["result"]), {"model_id": out.get("model_id", "?"), "latency_ms": out.get("latency_ms")}

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        out = await self.invoke({"task": "chat", **payload})
        if "error" in out:
            raise RuntimeError(f"agent error: {out['error']}")
        return {"reply": out.get("reply", ""), "intents": out.get("intents", [])}

    async def chat_ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        out = await self.invoke({"task": "chat_ack", **payload})
        if "error" in out:
            raise RuntimeError(f"agent error: {out['error']}")
        return {"needs_work": bool(out.get("needs_work", True)), "message": out.get("message", "")}
