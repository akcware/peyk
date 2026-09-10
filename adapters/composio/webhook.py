"""Composio event -> Observation. Both the WebSocket path and the webhook path end up in to_observation().

Signature scheme (verified against composio 0.21.1 source, Triggers._verify_webhook_signature):
  to_sign  = f"{webhook-id}.{webhook-timestamp}.{raw_body}"
  sig      = "v1," + base64(HMAC-SHA256(secret, to_sign))
Header names: webhook-id, webhook-timestamp (unix seconds), webhook-signature ("v1,... v1,..." allowed).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import UUID

from core.log import get_logger
from core.models import Observation

from .mappings import MAPPINGS, apply

log = get_logger("composio.webhook")

TRIGGER_EVENT_TYPE = "composio.trigger.message"


class WebhookSignatureError(Exception):
    pass


def sign(webhook_id: str, timestamp: str, body: str, secret: str) -> str:
    """Produce a valid signature header value (used by tests and local tooling)."""
    digest = hmac.new(secret.encode(), f"{webhook_id}.{timestamp}.{body}".encode(), hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def verify_signature(
    *, webhook_id: str, timestamp: str, body: str, signature: str, secret: str, tolerance: int = 300
) -> None:
    if not (webhook_id and timestamp and body and signature and secret):
        raise WebhookSignatureError("missing signature material")
    if tolerance > 0:
        try:
            ts = int(timestamp)
        except ValueError as e:
            raise WebhookSignatureError("bad timestamp") from e
        if abs(int(time.time()) - ts) > tolerance:
            raise WebhookSignatureError("timestamp outside tolerance")
    expected = sign(webhook_id, timestamp, body, secret)[3:]
    provided = [p[3:] for p in signature.split(" ") if p.startswith("v1,")]
    if not any(hmac.compare_digest(p, expected) for p in provided):
        raise WebhookSignatureError("invalid signature")


def parse_envelope(body: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Normalize a webhook envelope (V3, or legacy V1/V2) to {"trigger_slug", "payload", "metadata"}.
    Same shape as the SDK's TriggerEvent delivered over WebSocket, so to_observation() is shared."""
    data = json.loads(body) if isinstance(body, (str, bytes)) else body
    if data.get("type") and "metadata" in data and "data" in data:  # V3 envelope
        meta = data.get("metadata") or {}
        slug = meta.get("trigger_slug") if data["type"] == TRIGGER_EVENT_TYPE else data["type"]
        return {"trigger_slug": str(slug or ""), "payload": data.get("data") or {}, "metadata": meta}
    # legacy: {"appName","payload","metadata":{"triggerName",...}} or {"type": "gmail_new_gmail_message","data":...}
    meta = data.get("metadata") or {}
    slug = meta.get("triggerName") or meta.get("trigger_slug") or data.get("type") or ""
    payload = data.get("payload") if "payload" in data else data.get("data") or {}
    return {"trigger_slug": str(slug).upper(), "payload": payload or {}, "metadata": meta}


def to_observation(event: dict[str, Any], user_id: UUID) -> Observation | None:
    """TriggerEvent-shaped dict -> Observation, or None for unknown triggers (logged, dropped)."""
    slug = str(event.get("trigger_slug") or "").upper()
    mapping = MAPPINGS.get(slug)
    if mapping is None:
        log.warning("composio.unknown_trigger", trigger_slug=slug)
        return None
    mapped = apply(mapping, event.get("payload") or {})
    if mapped is None:
        log.warning("composio.missing_source_key", trigger_slug=slug)
        return None
    return Observation(user_id=user_id, **mapped)
