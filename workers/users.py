"""DB-backed UserDirectory + per-user notifier cache. The only place workers map channels to people."""
from __future__ import annotations

from uuid import UUID

from core import db
from core.adapter import AdapterRegistry
from core.config import Settings
from core.log import get_logger
from core.repo import user_repo

log = get_logger("workers.users")


class DbUserDirectory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def resolve_control(self, source: str, thread_key: str, *, display_name: str | None = None,
                              language: str | None = None) -> UUID:
        """New people start in the language their messaging client reports (fallback English) — never in the
        operator's USER_LANGUAGE; the agent adjusts later from how they write (set_profile)."""
        async with db.connection() as conn:
            user, created = await user_repo.get_or_create_by_control(
                conn, source, thread_key, display_name=display_name, language=language or "en",
                timezone=self._settings.TIMEZONE)
        if created:
            log.info("users.created", user_id=str(user["id"]), source=source, display_name=display_name)
        return user["id"]

    async def resolve_composio(self, composio_user_id: str) -> UUID | None:
        async with db.connection() as conn:
            user = await user_repo.get_by_composio(conn, composio_user_id)
        return user["id"] if user else None

    async def composio_user_id(self, user_id: UUID) -> str:
        async with db.connection() as conn:
            user = await user_repo.get(conn, user_id)
        return (user or {}).get("composio_user_id") or self._settings.COMPOSIO_USER_ID


async def ensure_bootstrap_user(settings: Settings) -> dict | None:
    """Migrates the single-user .env configuration into app_user (idempotent)."""
    async with db.connection() as conn:
        return await user_repo.ensure_bootstrap(
            conn, user_id=settings.USER_ID, control_source=settings.CONTROL_SOURCE, control_thread_key=settings.TELEGRAM_CHAT_ID,
            composio_user_id=settings.COMPOSIO_USER_ID, profile=settings.USER_PROFILE, language=settings.USER_LANGUAGE,
            timezone=settings.TIMEZONE)


class Notifiers:
    """Per-user Notifier cache: control adapter + thread key come from app_user."""

    def __init__(self, registry: AdapterRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings
        self._cache: dict[UUID, object] = {}

    async def for_user(self, conn, user_id: UUID):
        from workers.triage import Notifier  # local import: triage imports this module

        if user_id in self._cache:
            return self._cache[user_id]
        user = await user_repo.get(conn, user_id)
        if user is None:
            source, thread_key, lang = self.settings.CONTROL_SOURCE, self.settings.TELEGRAM_CHAT_ID, self.settings.USER_LANGUAGE
        else:
            source, thread_key, lang = user["control_source"], user["control_thread_key"], user.get("language") or self.settings.USER_LANGUAGE
        notifier = Notifier(self.registry.get(source), thread_key, user_id, language=lang)
        self._cache[user_id] = notifier
        return notifier

    def register(self, user_id: UUID, notifier) -> None:
        self._cache[user_id] = notifier
