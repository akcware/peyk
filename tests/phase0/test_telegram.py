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


class FakeTranscriber:
    def __init__(self, text: str = "yarın dokuzda toplantı", fail: Exception | None = None) -> None:
        self.text, self.fail, self.calls = text, fail, []

    async def transcribe(self, audio, *, media_encoding, sample_rate_hz, languages):
        from core.stt import Transcript
        self.calls.append({"bytes": len(audio), "encoding": media_encoding, "rate": sample_rate_hz, "languages": languages})
        if self.fail:
            raise self.fail
        return Transcript(text=self.text, language=languages[0], took_ms=12)


def _voice_update(update_id: int, duration: int = 4) -> dict:
    return {"update_id": update_id, "message": {
        "message_id": 3, "date": 3, "chat": {"id": 7}, "from": {"id": 7, "first_name": "Aslı", "language_code": "de"},
        "voice": {"file_id": "AwACAgQAAxk", "file_unique_id": "u1", "duration": duration, "mime_type": "audio/ogg", "file_size": 9000}}}


def _voice_http(updates: list[dict], seen: list) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/file/bott/voice/file_1.oga":           # the file endpoint, not a bot method
            seen.append(("download", None))
            return httpx.Response(200, content=b"OggS" + b"\0" * 500)
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        seen.append((method, payload))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": [] if payload.get("offset") else updates})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_id": payload["file_id"], "file_path": "voice/file_1.oga"}})
        return httpx.Response(200, json={"ok": True, "result": True})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org/bott")


async def _first(adapter: TelegramAdapter):
    conn = Connection(adapter_id="telegram", user_id=USER_ID)
    async for obs in adapter.subscribe(conn):
        return obs


class FakeDirectory:
    """The person writes to us in Turkish (stored language) although the phone's UI is German."""

    def __init__(self, language: str | None) -> None:
        self.language, self.seen_client_language = language, None

    async def resolve_control(self, source, thread_key, *, display_name=None, language=None):
        self.seen_client_language = language
        return USER_ID

    async def resolve_composio(self, composio_user_id):
        return None

    async def composio_user_id(self, user_id):
        return "default"

    async def language_of(self, user_id):
        return self.language


async def test_voice_note_becomes_text_for_the_agent(settings):
    from core.routing import control_event

    seen: list = []
    s = settings.model_copy(update={"TELEGRAM_BOT_TOKEN": "t", "STT_LANGUAGES": "en-US,de-DE,tr-TR"})
    stt = FakeTranscriber()
    adapter = TelegramAdapter(settings=s, http=_voice_http([_voice_update(20)], seen), poll_timeout=0, transcriber=stt)

    obs = await _first(adapter)
    assert [m for m, _ in seen] == ["getUpdates", "getFile", "download"]
    assert seen[1][1] == {"file_id": "AwACAgQAAxk"}
    assert stt.calls == [{"bytes": 504, "encoding": "ogg-opus", "rate": 48000, "languages": ["de-DE", "en-US", "tr-TR"]}]

    # with a directory, the language the person uses with us leads the hint, not the phone's UI language
    seen.clear()
    stt, users = FakeTranscriber(), FakeDirectory("tr")
    adapter = TelegramAdapter(settings=s, http=_voice_http([_voice_update(24)], seen), poll_timeout=0, transcriber=stt, users=users)
    obs = await _first(adapter)
    assert users.seen_client_language == "de" and obs.user_id == USER_ID
    assert stt.calls[0]["languages"] == ["tr-TR", "en-US", "de-DE"]
    ev = control_event(obs)                                   # what every worker reads
    assert ev.text == "[voice message, 4 s, automatic transcript] yarın dokuzda toplantı"
    assert ev.message_id == 3 and ev.display_name == "Aslı" and ev.callback is None
    assert obs.payload["voice"] == {"duration_s": 4, "file_id": "AwACAgQAAxk", "language": "tr-TR", "took_ms": 12, "chars": 22}
    assert obs.payload["message"]["voice"]["file_id"] == "AwACAgQAAxk"   # the raw update is kept


async def test_voice_note_failure_still_reaches_the_agent(settings):
    from core.routing import control_text

    seen: list = []
    s = settings.model_copy(update={"TELEGRAM_BOT_TOKEN": "t"})
    stt = FakeTranscriber(fail=RuntimeError("transcribe down"))
    obs = await _first(TelegramAdapter(settings=s, http=_voice_http([_voice_update(21)], seen), poll_timeout=0, transcriber=stt))
    assert control_text(obs) == "[voice message, 4 s, could not be transcribed: transcribe down]"
    assert obs.payload["voice"]["error"] == "transcribe down"

    # too long: never downloaded, the person is told
    seen.clear()
    stt = FakeTranscriber()
    s = s.model_copy(update={"VOICE_MAX_S": 60})
    obs = await _first(TelegramAdapter(settings=s, http=_voice_http([_voice_update(22, duration=61)], seen), poll_timeout=0, transcriber=stt))
    assert control_text(obs) == "[voice message, 61 s, could not be transcribed: longer than 60 s]"
    assert [m for m, _ in seen] == ["getUpdates"] and stt.calls == []

    # no transcriber wired at all (e.g. a stub deployment): same shape, nothing crashes
    obs = await _first(TelegramAdapter(settings=s, http=_voice_http([_voice_update(23)], []), poll_timeout=0))
    assert control_text(obs) == "[voice message, 4 s, could not be transcribed: no transcriber configured]"


class FakeDescriber:
    def __init__(self, text: str = "A shop listing: SimonsVoss Transponder 3064, normally 37 to 40 euro.", fail: Exception | None = None) -> None:
        self.text, self.fail, self.calls = text, fail, []

    async def describe(self, image, *, fmt, language, caption):
        from core.vision import Description
        self.calls.append({"bytes": len(image), "fmt": fmt, "language": language, "caption": caption})
        if self.fail:
            raise self.fail
        return Description(text=self.text, took_ms=7)


def _photo_update(update_id: int, caption: str | None = "Bu nedir", reply_to: dict | None = None, document: dict | None = None) -> dict:
    msg = {"message_id": 9, "date": 3, "chat": {"id": 7}, "from": {"id": 7, "first_name": "Aslı", "language_code": "tr"}}
    if document:
        msg["document"] = document
    else:
        msg["photo"] = [{"file_id": "small", "file_unique_id": "s", "width": 90, "height": 160, "file_size": 1200},
                        {"file_id": "big", "file_unique_id": "b", "width": 720, "height": 1280, "file_size": 88000}]
    if caption:
        msg["caption"] = caption
    if reply_to:
        msg["reply_to_message"] = reply_to
    return {"update_id": update_id, "message": msg}


def _file_http(updates: list[dict], seen: list, *, file_bytes: bytes = b"\xff\xd8" + b"j" * 500):
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if request.url.path.startswith("/file/"):
            seen.append(("download", request.url.path))
            return httpx.Response(200, content=file_bytes)
        payload = json.loads(request.content)
        seen.append((method, payload))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": [] if payload.get("offset") else updates})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_id": payload["file_id"], "file_path": f"photos/{payload['file_id']}.jpg"}})
        return httpx.Response(200, json={"ok": True, "result": True})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org/bott")


async def test_photo_becomes_text_for_the_agent(settings):
    from core.routing import agent_text, control_event

    s = settings.model_copy(update={"TELEGRAM_BOT_TOKEN": "t"})
    seen: list = []
    vision = FakeDescriber()
    quoted = {"message_id": 4, "from": {"id": 1, "is_bot": True, "first_name": "Peyk"}, "chat": {"id": 7},
              "text": "İki iş ilanı var bugün. MHP (Porsche şirketi) Junior AI Engineer arıyor."}
    adapter = TelegramAdapter(settings=s, http=_file_http([_photo_update(30, reply_to=quoted)], seen), poll_timeout=0,
                              describer=vision, users=FakeDirectory("tr"))
    obs = await _first(adapter)
    assert [m for m, _ in seen] == ["getUpdates", "getFile", "download"]
    assert seen[1][1] == {"file_id": "big"}                       # the largest size, not the thumbnail
    assert vision.calls == [{"bytes": 502, "fmt": "jpeg", "language": "tr", "caption": "Bu nedir"}]
    ev = control_event(obs)
    assert ev.text == "Bu nedir\n[photo, automatic description] A shop listing: SimonsVoss Transponder 3064, normally 37 to 40 euro."
    assert ev.reply_to == '[replying to your message: "İki iş ilanı var bugün. MHP (Porsche şirketi) Junior AI Engineer arıyor."]'
    assert agent_text(obs).startswith('[replying to your message: "İki iş ilanı') and agent_text(obs).endswith("40 euro.")
    assert ev.message_id == 9 and ev.display_name == "Aslı" and ev.callback is None
    assert obs.payload["image"] == {"file_id": "big", "mime_type": "image/jpeg", "file_size": 88000, "took_ms": 7, "chars": 68}

    # no caption, no vision model: the agent still gets the message, as a note it can relay; nothing downloaded
    seen.clear()
    adapter = TelegramAdapter(settings=s, http=_file_http([_photo_update(31, caption=None)], seen), poll_timeout=0)
    obs = await _first(adapter)
    assert [m for m, _ in seen] == ["getUpdates"]
    assert control_event(obs).text == "[photo, could not be viewed: no vision model configured]"
    assert obs.payload["image"]["error"] == "no vision model configured"

    # an image sent as a file keeps its type; a PDF "document" is not an image at all (plain message, no control)
    seen.clear()
    png = {"file_id": "doc1", "file_unique_id": "d", "mime_type": "image/png", "file_name": "shot.png", "file_size": 4000}
    adapter = TelegramAdapter(settings=s, http=_file_http([_photo_update(32, document=png)], seen), poll_timeout=0, describer=vision)
    obs = await _first(adapter)
    assert vision.calls[-1]["fmt"] == "png" and control_event(obs).text.startswith("Bu nedir\n[photo, automatic description]")
    pdf = {"file_id": "doc2", "file_unique_id": "e", "mime_type": "application/pdf", "file_name": "a.pdf", "file_size": 4000}
    assert TelegramAdapter.image_of(_photo_update(33, document=pdf)["message"]) is None

    # too large for the model: refused without a download, the person is told
    seen.clear()
    huge = _photo_update(34)
    huge["message"]["photo"][-1]["file_size"] = 5_000_000
    adapter = TelegramAdapter(settings=s, http=_file_http([huge], seen), poll_timeout=0, describer=vision)
    obs = await _first(adapter)
    assert [m for m, _ in seen] == ["getUpdates"] and "larger than the model takes" in control_event(obs).text


def test_reply_context_is_a_separate_field():
    from core.routing import agent_text, control_event, reply_context

    own = {"message_id": 2, "from": {"id": 7, "is_bot": False, "first_name": "Aslı"}, "text": "  Vezne   ne demek "}
    upd = {"update_id": 40, "message": {"message_id": 5, "chat": {"id": 7}, "from": {"id": 7, "first_name": "Aslı"},
                                        "text": "Bunun hakkında biraz daha bilgi alabilir miyim", "reply_to_message": own}}
    obs = TelegramAdapter.update_to_observation(upd, USER_ID)
    ev = control_event(obs)
    assert ev.text == "Bunun hakkında biraz daha bilgi alabilir miyim"       # commands and draft edits see the words only
    assert ev.reply_to == '[replying to their own earlier message: "Vezne ne demek"]'
    assert agent_text(obs) == '[replying to their own earlier message: "Vezne ne demek"]\nBunun hakkında biraz daha bilgi alabilir miyim'
    assert reply_context({"reply_to_message": {"from": {"is_bot": True}, "photo": [{}]}}) == '[replying to your message: "(a photo)"]'
    assert reply_context({"reply_to_message": {"from": {"is_bot": True}, "text": "x" * 700}}).endswith("x…\"]")
    assert reply_context({"text": "plain"}) is None and control_event(TelegramAdapter.update_to_observation({"update_id": 1, "message": {"text": "hi", "chat": {"id": 1}}}, USER_ID)).reply_to is None
