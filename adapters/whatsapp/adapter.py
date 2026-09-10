"""Phase 6 placeholder. Observations are written by the Go `sessions` process; subscribe() is empty by design."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

from core.adapter import Capabilities, NotSupported
from core.models import Connection, Content, Observation


class WhatsAppAdapter:
    id = "whatsapp"

    def capabilities(self) -> Capabilities:
        return {"can_send": True, "needs_session": True, "needs_user_device": False}

    async def connect(self, user_id: UUID) -> Connection:
        return Connection(adapter_id=self.id, user_id=user_id)

    async def subscribe(self, conn: Connection) -> AsyncIterator[Observation]:
        return
        yield

    async def backfill(self, conn: Connection, since: datetime) -> AsyncIterator[Observation]:
        return
        yield

    async def send(self, conn: Connection, thread_key: str, content: Content) -> str:
        raise NotSupported("whatsapp outbox arrives in phase 6")
