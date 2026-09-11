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


def user_language() -> str:
    """USER_LANGUAGE env as a full language name the model cannot misread ('tr' -> 'Turkish')."""
    code = os.environ.get("USER_LANGUAGE", "en").strip()
    return LANGUAGE_NAMES.get(code.lower(), code or "English")
