"""Images become text before any worker sees them, the way voice notes do (core/stt.py). One protocol
(`Describer`) so a channel adapter never knows which engine looks at the picture; one engine today: a
vision-capable chat model through the Bedrock Converse API (the chat model itself unless VISION_MODEL_ID says
otherwise — no extra IAM right, the same `bedrock:Converse` the chat already uses).

The description is written for an assistant that cannot see the image: what it shows, every readable text
verbatim (a screenshot's product name and prices, a letter's sender and dates), and what it most likely is.
Channel-agnostic (Telegram today, WhatsApp next)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from core.log import get_logger
from core.stt import LANGUAGE_NAMES

log = get_logger("vision")

# Bedrock Converse image formats, by the MIME type a channel reports. Telegram re-encodes photos as JPEG; an
# image sent "as file" keeps its own type.
IMAGE_FORMATS: dict[str, str] = {"image/jpeg": "jpeg", "image/jpg": "jpeg", "image/png": "png", "image/gif": "gif",
                                 "image/webp": "webp"}
MAX_IMAGE_BYTES = 3_750_000   # Bedrock Converse refuses larger images
MAX_CAPTION_IN_HINT = 300

DESCRIBE_INSTRUCTION = (
    "Describe this image for an assistant who cannot see it and has to answer the person's questions about it. "
    "Be concrete and complete: what it shows; every readable text verbatim (names, numbers, prices, dates, "
    "addresses, labels, message text); and what it most likely is (a screenshot of which app or site, a photo of "
    "what, which kind of document). Do not guess beyond what is visible; say when something is unreadable. "
    "No preamble, no opinions, no markdown. Write in {language}.{caption_hint}"
)


@dataclass
class Description:
    text: str
    took_ms: int = 0


class Describer(Protocol):
    async def describe(self, image: bytes, *, fmt: str, language: str | None, caption: str | None) -> Description: ...


def image_format(mime_type: str | None) -> str | None:
    """Bedrock format for a MIME type, None when the model cannot take it (a PDF, a HEIC, a video)."""
    return IMAGE_FORMATS.get((mime_type or "").split(";")[0].strip().lower())


def photo_text(description: str, caption: str | None = None) -> str:
    """What the agent reads for a photo: the person's caption (their words) first, then the bracketed automatic
    description, so the model knows it is looking at a description and not at the person's text."""
    text = " ".join(description.split())
    line = f"[photo, automatic description] {text}" if text else "[photo: nothing could be made out]"
    cap = (caption or "").strip()
    return f"{cap}\n{line}" if cap else line


def photo_failed_text(reason: str, caption: str | None = None) -> str:
    cap = (caption or "").strip()
    line = f"[photo, could not be viewed: {reason}]"
    return f"{cap}\n{line}" if cap else line


class BedrockVision:
    """A vision-capable model through the Bedrock Converse API. The instruction travels in the user turn after the
    image block, like the Voxtral call, so one code path serves every Bedrock model."""

    def __init__(self, *, region: str, model_id: str) -> None:
        self._region = region
        self._model_id = model_id
        self._client = None

    def _runtime(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client("bedrock-runtime", region_name=self._region,
                                        config=Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 2}))
        return self._client

    async def describe(self, image: bytes, *, fmt: str, language: str | None, caption: str | None) -> Description:
        t0 = time.perf_counter()
        lang = LANGUAGE_NAMES.get((language or "").split("-")[0].lower(), "the language of the text in the image, else English")
        cap = " ".join((caption or "").split())[:MAX_CAPTION_IN_HINT]
        hint = f' The person sent it with the words: "{cap}" — make sure what they may be asking about is covered.' if cap else ""
        content = [{"image": {"format": fmt, "source": {"bytes": image}}},
                   {"text": DESCRIBE_INSTRUCTION.format(language=lang, caption_hint=hint)}]

        def call():
            return self._runtime().converse(modelId=self._model_id, messages=[{"role": "user", "content": content}],
                                            inferenceConfig={"maxTokens": 1500, "temperature": 0})

        resp = await asyncio.to_thread(call)
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()
        took = int((time.perf_counter() - t0) * 1000)
        usage = resp.get("usage") or {}
        log.info("vision.described", model=self._model_id, format=fmt, bytes=len(image), chars=len(text), ms=took,
                 input_tokens=usage.get("inputTokens"), output_tokens=usage.get("outputTokens"))
        return Description(text=text, took_ms=took)


def make_describer(*, region: str, model_id: str) -> Describer | None:
    return BedrockVision(region=region, model_id=model_id) if model_id else None
