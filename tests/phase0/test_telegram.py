from __future__ import annotations

import json

import httpx

from adapters.telegram.adapter import TelegramAdapter
from core.models import Connection, Content
from tests.conftest import USER_ID


def test_update_to_observation_message():
    update = {"update_id": 900001, "message": {"message_id": 5, "date": 1789031632,
              "chat": {"id": 123456, "type": "private"}, "from": {"id": 123456, "first_name": "A"}, "text": "hi"}}
    obs = TelegramAdapter.update_to_observation(update, USER_ID)
    assert obs.source == "telegram" and obs.source_key == "900001" and obs.kind == "message_in"
    assert obs.thread_key == "123456" and obs.payload == update
    assert obs.occurred_at.isoformat().startswith("2026-09-10T09:13:52")


def test_update_to_observation_callback_and_unknown():
    cq = {"update_id": 900002, "callback_query": {"id": "cq1", "data": "useful:abc",
          "message": {"message_id": 6, "chat": {"id": 123456}}}}
    obs = TelegramAdapter.update_to_observation(cq, USER_ID)
    assert obs.thread_key == "123456" and obs.payload["callback_query"]["data"] == "useful:abc"
    assert TelegramAdapter.update_to_observation({"update_id": 1, "edited_message": {}}, USER_ID) is None


async def test_send_and_poll(settings):
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        seen.append((method, payload))
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": payload["chat_id"]}}})
        if method == "getUpdates":
            if payload.get("offset"):
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 10, "message": {"message_id": 1, "date": 1, "chat": {"id": 7}, "text": "a"}},
                {"update_id": 11, "message": {"message_id": 2, "date": 2, "chat": {"id": 7}, "text": "b"}},
            ]})
        return httpx.Response(200, json={"ok": True, "result": True})

    s = settings.model_copy(update={"TELEGRAM_BOT_TOKEN": "t"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org/bott")
    adapter = TelegramAdapter(settings=s, http=http, poll_timeout=0)
    conn = Connection(adapter_id="telegram", user_id=USER_ID)

    mid = await adapter.send(conn, "7", Content(text="hello", reply_markup={"inline_keyboard": []}))
    assert mid == "42" and seen[-1][1]["reply_markup"] == {"inline_keyboard": []}

    got = []
    async for obs in adapter.subscribe(conn):
        got.append(obs)
        if len(got) == 2:
            break
    assert [o.source_key for o in got] == ["10", "11"]
    assert adapter._offset == 12
