"""Speech to text for voice notes. One protocol (`Transcriber`) so a channel adapter never knows which engine
runs. Two engines, both on the AWS key the chat models already use:

- `TranscribeStreaming` (default): Amazon Transcribe streaming over HTTP/2. OGG/Opus straight in, no S3,
  language identified among the person's client language and STT_LANGUAGES. Needs
  `transcribe:StartStreamTranscription` on the runtime key.
- `VoxtralBedrock`: Mistral Voxtral through Bedrock Converse. Needs no right beyond `bedrock:Converse`; the
  audio is decoded to MP3 in memory first (Bedrock takes mp3/wav only). Kept for accounts where Transcribe
  is not available (the AWS free plan denies it).

Everything here is channel-agnostic (Telegram today, WhatsApp next)."""
from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass
from typing import Protocol

from core.log import get_logger

log = get_logger("stt")

# Client language (ISO 639-1, what Telegram/WhatsApp report) -> a Transcribe *streaming* locale. One dialect per
# language: streaming language identification refuses two dialects of the same language in one option list.
STREAMING_LOCALES: dict[str, str] = {
    "en": "en-US", "de": "de-DE", "tr": "tr-TR", "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "pt": "pt-BR",
    "ru": "ru-RU", "uk": "uk-UA", "ar": "ar-SA", "fa": "fa-IR", "nl": "nl-NL", "pl": "pl-PL", "sv": "sv-SE",
    "da": "da-DK", "fi": "fi-FI", "no": "no-NO", "nb": "no-NO", "cs": "cs-CZ", "el": "el-GR", "he": "he-IL",
    "hi": "hi-IN", "id": "id-ID", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN", "ro": "ro-RO", "hu": "hu-HU",
    "vi": "vi-VN", "th": "th-TH", "ms": "ms-MY", "tl": "tl-PH", "ca": "ca-ES", "sr": "sr-RS", "hr": "hr-HR",
    "sk": "sk-SK", "bg": "bg-BG", "lt": "lt-LT", "lv": "lv-LV", "et": "et-ET", "sl": "sl-SI", "ka": "ka-GE",
    "hy": "hy-AM", "az": "az-AZ", "kk": "kk-KZ", "uz": "uz-UZ", "bn": "bn-IN", "ta": "ta-IN", "te": "te-IN",
    "mr": "mr-IN", "gu": "gu-IN", "pa": "pa-IN", "sw": "sw-KE", "af": "af-ZA", "zu": "zu-ZA", "so": "so-SO",
}
LANGUAGE_NAMES: dict[str, str] = {   # for the hint given to Voxtral
    "en": "English", "de": "German", "tr": "Turkish", "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic", "fa": "Persian", "nl": "Dutch",
    "pl": "Polish", "sv": "Swedish", "da": "Danish", "fi": "Finnish", "no": "Norwegian", "cs": "Czech",
    "el": "Greek", "he": "Hebrew", "hi": "Hindi", "id": "Indonesian", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "ro": "Romanian", "hu": "Hungarian", "vi": "Vietnamese", "th": "Thai", "ms": "Malay",
    "ca": "Catalan", "sr": "Serbian", "hr": "Croatian", "sk": "Slovak", "bg": "Bulgarian", "az": "Azerbaijani",
}
MAX_LANGUAGE_OPTIONS = 5      # Transcribe streaming identification accepts at most five candidates
TELEGRAM_VOICE_ENCODING = "ogg-opus"
TELEGRAM_VOICE_SAMPLE_RATE = 48_000   # Opus always runs at 48 kHz; Telegram voice notes are mono OGG/Opus


@dataclass
class Transcript:
    text: str
    language: str | None = None   # locale the engine settled on ("tr-TR"), when it says
    took_ms: int = 0


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, *, media_encoding: str, sample_rate_hz: int,
                         languages: list[str]) -> Transcript: ...


def language_options(preferred: str | None, defaults: list[str]) -> list[str]:
    """Candidate locales for language identification: the person's client language first, then the configured
    defaults; at most five, one dialect per language, unknown codes dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for code in [STREAMING_LOCALES.get((preferred or "").split("-")[0].lower()), *defaults]:
        code = (code or "").strip()
        base = code.split("-")[0].lower()
        if not code or base in seen:
            continue
        seen.add(base)
        out.append(code)
    return out[:MAX_LANGUAGE_OPTIONS]


def voice_text(transcript: str, duration_s: int) -> str:
    """What the agent reads for a voice note. Bracketed like system events so the model knows it is an
    automatic transcript (and can quote back what it heard when unsure)."""
    text = " ".join(transcript.split()).strip("\"'“” ")
    if not text:
        return f"[voice message, {duration_s} s: nothing recognisable was said]"
    return f"[voice message, {duration_s} s, automatic transcript] {text}"


def voice_failed_text(duration_s: int, reason: str) -> str:
    return f"[voice message, {duration_s} s, could not be transcribed: {reason}]"


class TranscribeStreaming:
    """Amazon Transcribe streaming over HTTP/2. Credentials come from the same botocore chain as Bedrock
    (AWS_PROFILE / env keys), resolved per call so a rotated key is picked up without a restart."""

    CHUNK = 8 * 1024

    def __init__(self, *, region: str) -> None:
        self._region = region

    def _client(self):
        import botocore.session
        from amazon_transcribe.auth import StaticCredentialResolver
        from amazon_transcribe.client import TranscribeStreamingClient

        creds = botocore.session.get_session().get_credentials()
        if creds is None:
            raise RuntimeError("no AWS credentials for Transcribe")
        frozen = creds.get_frozen_credentials()
        resolver = StaticCredentialResolver(frozen.access_key, frozen.secret_key, frozen.token)
        return TranscribeStreamingClient(region=self._region, credential_resolver=resolver)

    async def transcribe(self, audio: bytes, *, media_encoding: str, sample_rate_hz: int,
                         languages: list[str]) -> Transcript:
        t0 = time.perf_counter()
        client = await asyncio.to_thread(self._client)   # credential resolution may touch disk or the network
        params: dict = {"media_sample_rate_hz": sample_rate_hz, "media_encoding": media_encoding}
        if len(languages) >= 2:
            params.update(language_code=None, identify_language=True, language_options=languages,
                          preferred_language=languages[0])
        else:
            params["language_code"] = languages[0] if languages else "en-US"
        stream = await client.start_stream_transcription(**params)

        async def feed() -> None:
            for i in range(0, len(audio), self.CHUNK):
                await stream.input_stream.send_audio_event(audio_chunk=audio[i:i + self.CHUNK])
            await stream.input_stream.end_stream()

        async def collect() -> tuple[str, str | None]:
            parts: list[str] = []
            lang: str | None = None
            async for event in stream.output_stream:
                transcript = getattr(event, "transcript", None)
                for r in (getattr(transcript, "results", None) or []):
                    if r.is_partial or not r.alternatives:
                        continue
                    parts.append(r.alternatives[0].transcript.strip())
                    lang = getattr(r, "language_code", None) or lang
            return " ".join(p for p in parts if p), lang

        _, (text, lang) = await asyncio.gather(feed(), collect())
        took = int((time.perf_counter() - t0) * 1000)
        log.info("stt.transcribed", engine="transcribe", bytes=len(audio), chars=len(text), language=lang, ms=took)
        return Transcript(text=text, language=lang, took_ms=took)


def to_mp3(audio: bytes, media_encoding: str) -> bytes:
    """Voxtral on Bedrock takes mp3 or wav only; Telegram sends OGG/Opus. libsndfile (bundled with soundfile)
    decodes Opus and encodes MP3 in memory, ~40 ms for a 30 s note, no ffmpeg in the image."""
    if media_encoding in ("mp3", "wav"):
        return audio
    import soundfile as sf

    with sf.SoundFile(io.BytesIO(audio)) as f:
        pcm = f.buffer_read(dtype="int16")
        rate, channels = f.samplerate, f.channels
    out = io.BytesIO()
    with sf.SoundFile(out, mode="w", samplerate=rate, channels=channels, format="MP3", subtype="MPEG_LAYER_III") as w:
        w.buffer_write(pcm, dtype="int16")
    return out.getvalue()


VOXTRAL_INSTRUCTION = ("Transcribe this audio verbatim in the language spoken. Keep the speaker's language, do not "
                       "translate, do not answer or comment, do not add anything. Output only the transcript, or "
                       "nothing if nothing intelligible is said. Likely languages: {hint}.")


class VoxtralBedrock:
    """Mistral Voxtral through the Bedrock Converse API. Bedrock refuses a system prompt next to audio for this
    model, so the instruction travels in the user turn after the audio block."""

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

    async def transcribe(self, audio: bytes, *, media_encoding: str, sample_rate_hz: int,
                         languages: list[str]) -> Transcript:
        t0 = time.perf_counter()
        mp3 = await asyncio.to_thread(to_mp3, audio, media_encoding)
        fmt = media_encoding if media_encoding in ("mp3", "wav") else "mp3"
        hint = ", ".join(LANGUAGE_NAMES.get(c.split("-")[0].lower(), c) for c in languages) or "unknown"
        content = [{"audio": {"format": fmt, "source": {"bytes": mp3}}},
                   {"text": VOXTRAL_INSTRUCTION.format(hint=hint)}]

        def call():
            return self._runtime().converse(modelId=self._model_id, messages=[{"role": "user", "content": content}],
                                            inferenceConfig={"maxTokens": 2000, "temperature": 0})

        resp = await asyncio.to_thread(call)
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()
        took = int((time.perf_counter() - t0) * 1000)
        usage = resp.get("usage") or {}
        log.info("stt.transcribed", engine="voxtral", model=self._model_id, bytes=len(audio), mp3_bytes=len(mp3),
                 chars=len(text), ms=took, input_tokens=usage.get("inputTokens"), output_tokens=usage.get("outputTokens"))
        return Transcript(text=text, language=None, took_ms=took)


def make_transcriber(engine: str, *, region: str, model_id: str) -> Transcriber:
    if engine == "voxtral":
        return VoxtralBedrock(region=region, model_id=model_id)
    return TranscribeStreaming(region=region)
