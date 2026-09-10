"""Single source of configuration. Everything comes from .env / environment; no constants in code."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    USER_ID: UUID = UUID("00000000-0000-4000-8000-000000000001")
    USER_PROFILE: str = ""

    DATABASE_URL: str = "postgresql://agent:agent@localhost:5433/agent"
    TEST_DATABASE_URL: str | None = None

    ADAPTERS: str = "composio,telegram"
    # The user's own control channel: messages/callbacks from here are commands or chat, never triaged.
    CONTROL_SOURCE: str = "telegram"

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    COMPOSIO_API_KEY: str = ""
    COMPOSIO_WEBHOOK_SECRET: str = ""
    COMPOSIO_USER_ID: str = "default"
    COMPOSIO_GMAIL_AUTH_CONFIG_ID: str = ""
    COMPOSIO_GMAIL_CONNECTED_ACCOUNT_ID: str = ""
    COMPOSIO_DELIVERY: Literal["ws", "webhook"] = "ws"

    MODEL_PROVIDER: Literal["bedrock", "anthropic"] = "bedrock"
    AWS_REGION: str = "us-east-1"
    TRIAGE_MODEL_ID: str = ""
    CHAT_MODEL_ID: str = ""
    ANTHROPIC_API_KEY: str = ""
    AGENT_MODE: Literal["local", "agentcore"] = "local"
    AGENTCORE_RUNTIME_ARN: str = ""

    TIMEZONE: str = "Europe/Berlin"            # for daily@HH:MM recurrences and /remind HH:MM
    MORNING_BRIEF_AT: str = "08:00"            # local time; empty string disables the default brief job
    RECONCILE_EVERY: str = "10m"               # empty string disables the reconcile job
    RECONCILE_LOOKBACK_MINUTES: int = 15

    LOG_LEVEL: str = "INFO"
    DEFAULT_PHONE_REGION: str = Field(default="DE", description="phonenumbers default region")

    @property
    def adapter_ids(self) -> list[str]:
        return [a.strip() for a in self.ADAPTERS.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
