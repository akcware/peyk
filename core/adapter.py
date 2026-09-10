"""SourceAdapter protocol + registry. Routing is by data (capabilities), never by `if source == ...`."""
from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, TypedDict, runtime_checkable
from uuid import UUID

from core.models import Connection, Content, Observation


class Capabilities(TypedDict):
    can_send: bool
    needs_session: bool
    needs_user_device: bool


class NotSupported(Exception):
    """Adapter cannot perform this operation for this source/thread."""


@runtime_checkable
class SourceAdapter(Protocol):
    id: str

    async def connect(self, user_id: UUID) -> Connection: ...

    def subscribe(self, conn: Connection) -> AsyncIterator[Observation]: ...

    def backfill(self, conn: Connection, since: datetime) -> AsyncIterator[Observation]: ...

    async def send(self, conn: Connection, thread_key: str, content: Content) -> str: ...

    def capabilities(self) -> Capabilities: ...


# adapter id -> "module:Class". Adding a source = adding a line here + ADAPTERS env.
ADAPTER_MODULES: dict[str, str] = {
    "composio": "adapters.composio.adapter:ComposioAdapter",
    "telegram": "adapters.telegram.adapter:TelegramAdapter",
    "whatsapp": "adapters.whatsapp.adapter:WhatsAppAdapter",
}


# outbound channel -> adapter id that can send to it. Data, so workers never branch on channel.
CHANNEL_ADAPTERS: dict[str, str] = {
    "gmail": "composio",
    "calendar": "composio",
    "telegram": "telegram",
    "whatsapp": "whatsapp",
}


def validate_capabilities(caps: object) -> Capabilities:
    if not isinstance(caps, dict):
        raise TypeError("capabilities() must return a dict")
    expected = {"can_send", "needs_session", "needs_user_device"}
    if set(caps) != expected:
        raise ValueError(f"capabilities keys must be exactly {sorted(expected)}, got {sorted(caps)}")
    for k, v in caps.items():
        if not isinstance(v, bool):
            raise TypeError(f"capabilities[{k!r}] must be bool")
    return caps  # type: ignore[return-value]


class AdapterRegistry:
    """Loads adapters listed in config (ADAPTERS=composio,telegram). No source-specific branches."""

    def __init__(self, adapters: dict[str, SourceAdapter] | None = None) -> None:
        self._adapters: dict[str, SourceAdapter] = dict(adapters or {})

    @classmethod
    def from_ids(cls, ids: list[str], **kwargs) -> AdapterRegistry:
        adapters: dict[str, SourceAdapter] = {}
        for adapter_id in ids:
            spec = ADAPTER_MODULES.get(adapter_id)
            if spec is None:
                raise KeyError(f"unknown adapter id {adapter_id!r}; known: {sorted(ADAPTER_MODULES)}")
            module_name, class_name = spec.split(":")
            klass = getattr(importlib.import_module(module_name), class_name)
            adapter = klass(**kwargs.get(adapter_id, {}))
            validate_capabilities(adapter.capabilities())
            adapters[adapter.id] = adapter
        return cls(adapters)

    def register(self, adapter: SourceAdapter) -> None:
        validate_capabilities(adapter.capabilities())
        self._adapters[adapter.id] = adapter

    def get(self, adapter_id: str) -> SourceAdapter:
        return self._adapters[adapter_id]

    def all(self) -> list[SourceAdapter]:
        return list(self._adapters.values())

    def ingestable(self) -> list[SourceAdapter]:
        """Adapters whose subscribe() we drive from workers. Session adapters (needs_session) write
        observations from their own process, so no ingest task is opened for them."""
        return [a for a in self._adapters.values() if not a.capabilities()["needs_session"]]

    def for_channel(self, channel: str) -> SourceAdapter:
        adapter_id = CHANNEL_ADAPTERS.get(channel)
        if adapter_id is None or adapter_id not in self._adapters:
            raise KeyError(f"no adapter configured for channel {channel!r}")
        adapter = self._adapters[adapter_id]
        if not adapter.capabilities()["can_send"]:
            raise KeyError(f"adapter {adapter_id!r} cannot send")
        return adapter

    def senders(self) -> list[SourceAdapter]:
        return [a for a in self._adapters.values() if a.capabilities()["can_send"]]
