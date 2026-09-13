"""Images for the agent: the pure helpers always; the Bedrock engine only with real AWS credentials
(llm marker: `AWS_PROFILE=<admin or peyk> pytest -m llm tests/phase0/test_vision.py`)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.vision import BedrockVision, image_format, make_describer, photo_failed_text, photo_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_image_format_maps_mime_types_the_model_takes():
    assert image_format("image/jpeg") == "jpeg" and image_format("image/png") == "png"
    assert image_format("IMAGE/WEBP; charset=x") == "webp"
    assert image_format("application/pdf") is None and image_format(None) is None and image_format("image/heic") is None


def test_photo_text_keeps_the_caption_first_and_brackets_the_description():
    assert photo_text("A screenshot of a  shop\nlisting.", "Bu nedir") == "Bu nedir\n[photo, automatic description] A screenshot of a shop listing."
    assert photo_text("A cat.") == "[photo, automatic description] A cat."
    assert photo_text("   ") == "[photo: nothing could be made out]"
    assert photo_failed_text("no vision model configured", "bak") == "bak\n[photo, could not be viewed: no vision model configured]"
    assert make_describer(region="us-east-1", model_id="") is None


@pytest.mark.llm
async def test_bedrock_vision_reads_a_screenshot():
    model_id = os.environ.get("VISION_MODEL_ID") or os.environ.get("CHAT_MODEL_ID")
    if not model_id:
        pytest.skip("CHAT_MODEL_ID not set")
    engine = BedrockVision(region=os.environ.get("AWS_REGION", "us-east-1"), model_id=model_id)
    d = await engine.describe((FIXTURES / "photo_listing.png").read_bytes(), fmt="png", language="tr", caption="Bu nedir")
    assert "3064" in d.text and ("37" in d.text or "40" in d.text), d.text
