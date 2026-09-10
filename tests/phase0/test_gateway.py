from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from adapters.composio.webhook import sign
from core.repo import observation_repo
from gateway import app as gateway_app
from tests.conftest import USER_ID

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "composio_events"


@pytest.fixture
def client(settings, pool, test_db_url, monkeypatch):
    monkeypatch.setattr(gateway_app, "get_settings", lambda: settings)
    app = gateway_app.create_app(test_db_url)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


def _headers(body: str, secret: str, *, webhook_id: str = "msg_test_1", ts: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    return {"webhook-id": webhook_id, "webhook-timestamp": ts, "webhook-signature": sign(webhook_id, ts, body, secret),
            "content-type": "application/json"}


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_webhook_signature(client, settings, conn):
    body = (FIX / "gmail_new_message.json").read_text()
    secret = settings.COMPOSIO_WEBHOOK_SECRET

    bad = await client.post("/webhook/composio", content=body, headers=_headers(body, "wrong-secret"))
    assert bad.status_code == 401
    assert await observation_repo.count(conn, USER_ID) == 0

    stale = await client.post("/webhook/composio", content=body, headers=_headers(body, secret, ts=str(int(time.time()) - 3600)))
    assert stale.status_code == 401

    tampered = json.dumps({**json.loads(body), "data": {**json.loads(body)["data"], "subject": "hacked"}})
    r = await client.post("/webhook/composio", content=tampered, headers=_headers(body, secret))
    assert r.status_code == 401

    ok = await client.post("/webhook/composio", content=body, headers=_headers(body, secret))
    assert ok.status_code == 200 and ok.json() == {"stored": True}
    assert await observation_repo.count(conn, USER_ID, source="gmail") == 1

    again = await client.post("/webhook/composio", content=body, headers=_headers(body, secret, webhook_id="msg_test_2"))
    assert again.status_code == 200 and again.json() == {"stored": False}
    assert await observation_repo.count(conn, USER_ID, source="gmail") == 1


async def test_unknown_trigger_returns_200_without_row(client, settings, conn):
    ev = json.loads((FIX / "gmail_new_message.json").read_text())
    ev["metadata"]["trigger_slug"] = "GITHUB_STAR_ADDED_EVENT"
    body = json.dumps(ev)
    r = await client.post("/webhook/composio", content=body, headers=_headers(body, settings.COMPOSIO_WEBHOOK_SECRET))
    assert r.status_code == 200 and r.json()["stored"] is False
    assert await observation_repo.count(conn, USER_ID) == 0
