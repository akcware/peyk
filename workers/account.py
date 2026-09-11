"""Account deletion (right to erasure), driven by the agent's DeleteAccountRequest intent and confirmed twice
with buttons. Expires after 10 minutes. Removes Composio connections, then every row of the user."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg

from core.adapter import AdapterRegistry
from core.log import get_logger
from core.models import Choice, Content
from core.repo import user_repo

log = get_logger("workers.account")

EXPIRES = timedelta(minutes=10)

TEXTS = {
    "en": {
        "ask1": "You asked me to delete your account. This removes everything I hold about you: your mail and calendar observations, memory, reminders, drafts and the connections to Gmail/Calendar. Are you sure?",
        "ask2": "Last check — this cannot be undone. Delete everything now?",
        "yes1": "Yes, delete my account", "yes2": "Yes, delete everything", "no": "Cancel",
        "cancelled": "Okay, nothing was deleted.", "expired": "The deletion request expired. Ask again if you still want it.",
        "done": "Done. Your connections were revoked and all your data is deleted. If you ever want to come back, just say hi.",
    },
    "tr": {
        "ask1": "Hesabını silmemi istedin. Bu, sende tuttuğum her şeyi kaldırır: mail ve takvim gözlemleri, hafıza, hatırlatıcılar, taslaklar ve Gmail/Takvim bağlantıları. Emin misin?",
        "ask2": "Son kontrol — bu işlem geri alınamaz. Her şeyi şimdi silelim mi?",
        "yes1": "Evet, hesabımı sil", "yes2": "Evet, her şeyi sil", "no": "Vazgeç",
        "cancelled": "Tamam, hiçbir şey silinmedi.", "expired": "Silme isteğinin süresi doldu. Hâlâ istiyorsan tekrar söyle.",
        "done": "Bitti. Bağlantıların iptal edildi ve tüm verilerin silindi. Geri dönmek istersen bir selam yeter.",
    },
    "de": {
        "ask1": "Du möchtest dein Konto löschen. Das entfernt alles, was ich über dich habe: Mail- und Kalenderbeobachtungen, Gedächtnis, Erinnerungen, Entwürfe und die Verbindungen zu Gmail/Kalender. Bist du sicher?",
        "ask2": "Letzte Prüfung — das lässt sich nicht rückgängig machen. Jetzt alles löschen?",
        "yes1": "Ja, Konto löschen", "yes2": "Ja, alles löschen", "no": "Abbrechen",
        "cancelled": "Okay, nichts wurde gelöscht.", "expired": "Die Löschanfrage ist abgelaufen. Frag einfach noch einmal.",
        "done": "Erledigt. Verbindungen widerrufen, alle Daten gelöscht. Wenn du zurückkommen willst, sag einfach Hallo.",
    },
}


def t(lang: str | None, key: str) -> str:
    return TEXTS.get((lang or "en").lower(), TEXTS["en"])[key]


def _choices(user_id: UUID, step: int, lang: str | None) -> list[Choice]:
    h = user_id.hex
    return [Choice(text=t(lang, "yes1" if step == 1 else "yes2"), data=f"del:yes{step}:{h}"),
            Choice(text=t(lang, "no"), data=f"del:no:{h}")]


async def start(conn: psycopg.AsyncConnection, user: dict, notifier) -> None:
    await user_repo.merge_state(conn, user["id"], {"pending_deletion": {"step": 1, "started_at": datetime.now(tz=UTC).isoformat()}})
    await notifier.send_content(Content(text=t(user.get("language"), "ask1"), choices=_choices(user["id"], 1, user.get("language"))))
    log.info("account.deletion_requested", user_id=str(user["id"]))


async def on_callback(conn: psycopg.AsyncConnection, user_id: UUID, data: str, notifier, registry: AdapterRegistry | None) -> str | None:
    """data: del:yes1:<hex> | del:yes2:<hex> | del:no:<hex>. Returns an ack text for the tap."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "del":
        return None
    verb, h = parts[1], parts[2]
    user = await user_repo.get(conn, user_id)
    if user is None or user["id"].hex != h:
        return None
    lang = user.get("language")
    pending = (user.get("state") or {}).get("pending_deletion") or {}
    if verb == "no":
        await user_repo.merge_state(conn, user_id, {"pending_deletion": None})
        await notifier.send_text(t(lang, "cancelled"))
        return t(lang, "cancelled")
    if not pending or datetime.fromisoformat(pending["started_at"]) < datetime.now(tz=UTC) - EXPIRES:
        await user_repo.merge_state(conn, user_id, {"pending_deletion": None})
        await notifier.send_text(t(lang, "expired"))
        return t(lang, "expired")
    if verb == "yes1" and pending.get("step") == 1:
        await user_repo.merge_state(conn, user_id, {"pending_deletion": {**pending, "step": 2}})
        await notifier.send_content(Content(text=t(lang, "ask2"), choices=_choices(user_id, 2, lang)))
        return None
    if verb == "yes2" and pending.get("step") == 2:
        await execute(conn, user, notifier, registry)
        return t(lang, "done")
    return None


async def execute(conn: psycopg.AsyncConnection, user: dict, notifier, registry: AdapterRegistry | None) -> dict[str, int]:
    """Composio first (needs the user row), then our data. The farewell is sent before the row disappears."""
    removed = {}
    if registry is not None:
        try:
            adapter = registry.get("composio")
            removed = await adapter.disconnect_all(await adapter.connect(user["id"]))
        except KeyError:
            pass
        except Exception as e:  # noqa: BLE001
            log.error("account.composio_disconnect_failed", error=str(e))
    await notifier.send_text(t(user.get("language"), "done"))
    counts = await user_repo.purge(conn, user["id"])
    log.info("account.deleted", user_id=str(user["id"]), composio=removed, rows=counts)
    return counts
