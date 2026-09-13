"""Model factory. Model IDs and provider come from .env; nothing hard-coded. Bedrock by default,
AnthropicModel for local development when Bedrock access is not available yet."""
from __future__ import annotations

import os
from typing import Literal

Kind = Literal["triage", "chat"]


def model_id(kind: Kind) -> str:
    key = "TRIAGE_MODEL_ID" if kind == "triage" else "CHAT_MODEL_ID"
    value = os.environ.get(key, "")
    if not value:
        raise RuntimeError(f"{key} is not set (.env)")
    return value


def build_model(kind: Kind, *, temperature: float = 0.0, max_tokens: int = 1024):
    """Returns a Strands model object. Reads env directly so agent/ has no dependency on core.config
    (the package must stay deployable to AgentCore on its own)."""
    provider = os.environ.get("MODEL_PROVIDER", "bedrock")
    mid = model_id(kind)
    if provider == "anthropic":
        from strands.models.anthropic import AnthropicModel

        return AnthropicModel(
            client_args={"api_key": os.environ["ANTHROPIC_API_KEY"]},
            model_id=mid,
            max_tokens=max_tokens,
            params={"temperature": temperature},
        )
    from strands.models.bedrock import BedrockModel

    return BedrockModel(
        model_id=mid,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=False,
    )


LANGUAGE_NAMES = {"tr": "Turkish", "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
                  "nl": "Dutch", "pt": "Portuguese", "ru": "Russian", "ar": "Arabic", "ja": "Japanese", "zh": "Chinese"}


def user_language(code: str | None = None) -> str:
    """Language code -> full name the model cannot misread ('tr' -> 'Turkish'). Falls back to USER_LANGUAGE env."""
    code = (code or os.environ.get("USER_LANGUAGE", "en")).strip()
    return LANGUAGE_NAMES.get(code.lower(), code or "English")


def user_profile(payload: dict | None = None) -> str:
    """The "about the person" block: name (how to address them), their own mail addresses, profile. From the
    request payload only. Never fall back to the process environment: in a multi-user deployment that would
    show one person's profile to another."""
    u = (payload or {}).get("user") or {}
    lines = []
    name = (u.get("display_name") or "").strip()
    if name:
        lines.append(f"Name: {name} — address them by their first name (never by the surname alone).")
    emails = [str(e) for e in (u.get("emails") or []) if e]
    if emails:
        lines.append(f"Their own mail addresses: {', '.join(emails)}. Mail FROM one of these is something they wrote themselves "
                     "(their reply in a thread) — never a request or a message to them.")
    lines.append((u.get("profile") or "").strip() or "(no profile yet — the person has not told you about themselves)")
    return "\n".join(lines)


def retry_strategy():
    """Short retry budget: Bedrock throttling must surface within ~30 s, not after minutes of silent backoff
    (Strands' default is 6 attempts, 4→240 s delays). Throttles are logged so they are visible in production."""
    import logging

    from strands.event_loop._retry import ModelRetryStrategy

    log = logging.getLogger("peyk.model")

    class LoggingRetry(ModelRetryStrategy):
        def is_retryable(self, exception: Exception) -> bool:
            retryable = super().is_retryable(exception)
            if retryable:
                log.warning("bedrock throttled; retrying (%s)", type(exception).__name__)
            return retryable

    return LoggingRetry(max_attempts=4, initial_delay=2, max_delay=12)
