"""Speech to text: the pure helpers and the in-memory decoder always; the engines only with real AWS credentials
(llm marker: `AWS_PROFILE=<admin or peyk> pytest -m llm tests/phase0/test_stt.py`)."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from core.stt import (
    MAX_LANGUAGE_OPTIONS,
    TELEGRAM_VOICE_ENCODING,
    TELEGRAM_VOICE_SAMPLE_RATE,
    TranscribeStreaming,
    VoxtralBedrock,
    language_options,
    make_transcriber,
    to_mp3,
    voice_failed_text,
    voice_text,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DEFAULTS = ["en-US", "de-DE", "tr-TR", "fr-FR", "es-ES"]
# Synthetic voice notes: macOS `say` -> ffmpeg libopus 48 kHz mono OGG, the shape Telegram sends.
CLIPS = [("voice_tr.ogg", "Mara", "tr"), ("voice_en.ogg", "Mara", "en")]


def test_language_options_puts_client_language_first_one_dialect_per_language():
    assert language_options("tr", DEFAULTS) == ["tr-TR", "en-US", "de-DE", "fr-FR", "es-ES"]
    assert language_options("pt-br", DEFAULTS)[:2] == ["pt-BR", "en-US"]      # region suffix ignored, five max
    assert len(language_options("pt", DEFAULTS)) == MAX_LANGUAGE_OPTIONS
    assert language_options(None, ["en-US", "en-GB", " de-DE "]) == ["en-US", "de-DE"]   # second English dialect dropped
    assert language_options("xx", []) == []                                   # unknown code, nothing configured


def test_voice_text_is_bracketed_and_normalised():
    assert voice_text("  merhaba   dünya \n", 4) == "[voice message, 4 s, automatic transcript] merhaba dünya"
    assert voice_text('"quoted"', 2) == "[voice message, 2 s, automatic transcript] quoted"
    assert voice_text("", 3) == "[voice message, 3 s: nothing recognisable was said]"
    assert voice_failed_text(700, "longer than 600 s") == "[voice message, 700 s, could not be transcribed: longer than 600 s]"


def test_make_transcriber_picks_engine():
    assert isinstance(make_transcriber("transcribe", region="us-east-1", model_id="m"), TranscribeStreaming)
    assert isinstance(make_transcriber("voxtral", region="us-east-1", model_id="m"), VoxtralBedrock)


def test_ogg_opus_decodes_to_mp3_in_memory():
    import soundfile as sf

    ogg = (FIXTURES / "voice_tr.ogg").read_bytes()
    mp3 = to_mp3(ogg, TELEGRAM_VOICE_ENCODING)
    assert (mp3[:3] == b"ID3" or mp3[:2] in (b"\xff\xfb", b"\xff\xf3")) and len(mp3) > 10_000   # ID3 tag or MPEG frame sync
    with sf.SoundFile(io.BytesIO(mp3)) as f:
        assert f.samplerate == TELEGRAM_VOICE_SAMPLE_RATE and f.channels == 1 and 4.0 < f.frames / f.samplerate < 5.5
    assert to_mp3(b"already", "mp3") == b"already"       # passthrough for formats Bedrock accepts


@pytest.mark.llm
@pytest.mark.parametrize("clip, expect, lang", CLIPS)
async def test_transcribe_streaming_real(clip: str, expect: str, lang: str):
    """Needs credentials with transcribe:StartStreamTranscription (the AWS free plan denies the service)."""
    t = await TranscribeStreaming(region="us-east-1").transcribe(
        (FIXTURES / clip).read_bytes(), media_encoding=TELEGRAM_VOICE_ENCODING,
        sample_rate_hz=TELEGRAM_VOICE_SAMPLE_RATE, languages=language_options(lang, DEFAULTS))
    assert expect.lower() in t.text.lower(), t.text
    assert t.language in (f"{lang}-TR" if lang == "tr" else "en-US", None)
    assert 0 < t.took_ms < 30_000


@pytest.mark.llm
@pytest.mark.parametrize("clip, expect, lang", CLIPS)
async def test_voxtral_bedrock_real(clip: str, expect: str, lang: str):
    """Needs bedrock:Converse and access to the Voxtral Small model in us-east-1."""
    t = await VoxtralBedrock(region="us-east-1", model_id="mistral.voxtral-small-24b-2507").transcribe(
        (FIXTURES / clip).read_bytes(), media_encoding=TELEGRAM_VOICE_ENCODING,
        sample_rate_hz=TELEGRAM_VOICE_SAMPLE_RATE, languages=language_options(lang, DEFAULTS))
    assert expect.lower() in t.text.lower(), t.text
    assert "remind" in t.text.lower() or "hatırlat" in t.text.lower(), t.text   # transcribed, not answered
    assert 0 < t.took_ms < 30_000
