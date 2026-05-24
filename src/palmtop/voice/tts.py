"""Text-to-speech providers for the agent's voice replies.

Supports:
  - Gemini Flash (cloud, reuses GOOGLE_API_KEY — recommended)
  - OpenAI TTS (cloud, requires OPENAI_API_KEY)

the agent responds with both a text message and a voice note in Telegram.

Gemini returns raw PCM (audio/L16) which is huge and unplayable by Telegram.
We transcode to OGG Opus via ffmpeg for small files that Telegram plays natively.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> Path | None:
        """Synthesize speech from text. Returns path to audio file, or None."""
        ...


def _parse_pcm_rate(mime: str) -> int:
    """Extract sample rate from mime like 'audio/L16;codec=pcm;rate=24000'."""
    for param in mime.split(";"):
        param = param.strip()
        if param.startswith("rate="):
            try:
                return int(param.split("=", 1)[1])
            except ValueError:
                pass
    return 24000


def _pcm_to_wav(pcm_bytes: bytes, rate: int) -> Path:
    """Wrap raw PCM in a proper WAV header (stdlib, zero deps).

    Returns a playable WAV file. Used as a fallback when ffmpeg isn't
    available — Telegram can play it via send_audio (not voice bubble).
    """
    import wave

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    wav_path = Path(tmp.name)

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)        # mono
        wf.setsampwidth(2)        # 16-bit = 2 bytes
        wf.setframerate(rate)
        wf.writeframes(pcm_bytes)

    log.info("Wrapped PCM→WAV: %d bytes → %d bytes", len(pcm_bytes), wav_path.stat().st_size)
    return wav_path


async def _pcm_to_ogg(pcm_bytes: bytes, mime: str) -> Path | None:
    """Transcode raw PCM audio to OGG Opus via ffmpeg.

    Gemini returns audio/L16;codec=pcm;rate=24000 — headerless 16-bit
    signed little-endian mono PCM.  We pipe it through ffmpeg to get a
    compact OGG Opus file that Telegram plays as a voice note.

    Returns None if ffmpeg isn't available (caller falls back to WAV).
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    rate = str(_parse_pcm_rate(mime))

    ogg_tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    ogg_tmp.close()
    ogg_path = Path(ogg_tmp.name)

    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y",
            "-f", "s16le",        # raw signed 16-bit little-endian
            "-ar", rate,          # sample rate from Gemini
            "-ac", "1",           # mono
            "-i", "pipe:0",      # read from stdin
            "-c:a", "libopus",
            "-b:a", "32k",       # 32 kbps — plenty for speech
            "-application", "voip",
            str(ogg_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=pcm_bytes), timeout=30,
        )

        if proc.returncode != 0:
            log.warning("ffmpeg PCM→OGG failed: %s", stderr.decode()[:200])
            ogg_path.unlink(missing_ok=True)
            return None

        log.info(
            "Transcoded PCM→OGG: %d bytes → %d bytes",
            len(pcm_bytes), ogg_path.stat().st_size,
        )
        return ogg_path

    except asyncio.TimeoutError:
        log.warning("ffmpeg PCM→OGG timed out")
        ogg_path.unlink(missing_ok=True)
        return None
    except Exception:
        log.exception("ffmpeg PCM→OGG error")
        ogg_path.unlink(missing_ok=True)
        return None


class GeminiTTS:
    """TTS via Gemini's native audio generation.

    Uses responseModalities: ["AUDIO"] to get Gemini to speak the text.
    Returns a WAV file that can be sent as a Telegram voice message.
    """

    API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash-preview-tts",
        voice: str = "Kore",
    ) -> None:
        if not api_key:
            raise ValueError("Google API key required for Gemini TTS")
        self._api_key = api_key
        self._model = model
        self._voice = voice

        import httpx
        self._client = httpx.AsyncClient(timeout=60.0)
        log.info("GeminiTTS ready: model=%s, voice=%s", self._model, self._voice)

    _MAX_RETRIES = 3
    _RETRY_CODES = {500, 502, 503, 429}

    async def close(self) -> None:
        await self._client.aclose()

    async def synthesize(self, text: str) -> Path | None:
        # Truncate very long text — TTS shouldn't read a novel
        if len(text) > 3000:
            text = text[:3000] + "... message truncated."

        try:
            body = {
                "contents": [{
                    "parts": [{"text": text}],
                }],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": self._voice,
                            }
                        }
                    },
                },
            }

            url = f"{self.API_URL}/{self._model}:generateContent"

            data = None
            for attempt in range(1, self._MAX_RETRIES + 1):
                resp = await self._client.post(
                    url,
                    headers={
                        "content-type": "application/json",
                        "x-goog-api-key": self._api_key,
                    },
                    json=body,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    break

                if resp.status_code not in self._RETRY_CODES or attempt == self._MAX_RETRIES:
                    log.warning("Gemini TTS error %d (attempt %d/%d): %s",
                                resp.status_code, attempt, self._MAX_RETRIES, resp.text[:200])
                    return None

                delay = 2 ** attempt  # 2s, 4s
                log.info("Gemini TTS %d, retrying in %ds (%d/%d)",
                         resp.status_code, delay, attempt, self._MAX_RETRIES)
                await asyncio.sleep(delay)

            if data is None:
                return None
            candidates = data.get("candidates", [])
            if not candidates:
                log.warning("Gemini TTS returned no candidates")
                return None

            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                log.warning("Gemini TTS returned empty parts")
                return None

            # Find the audio part
            for part in parts:
                inline = part.get("inlineData", {})
                if inline.get("mimeType", "").startswith("audio/"):
                    audio_b64 = inline.get("data", "")
                    if not audio_b64:
                        continue

                    audio_bytes = base64.standard_b64decode(audio_b64)
                    mime = inline["mimeType"]
                    log.info(
                        "Gemini TTS: %d bytes (%s) for %d chars",
                        len(audio_bytes), mime, len(text),
                    )

                    # Gemini returns raw PCM (audio/L16) — transcode to
                    # OGG Opus so Telegram can play it and the file is tiny.
                    if "L16" in mime or "pcm" in mime:
                        ogg_path = await _pcm_to_ogg(audio_bytes, mime)
                        if ogg_path:
                            return ogg_path
                        # ffmpeg not available — wrap in WAV header so it's
                        # at least a playable file (send_audio, not voice bubble)
                        log.info("ffmpeg unavailable, falling back to WAV")
                        return _pcm_to_wav(audio_bytes, _parse_pcm_rate(mime))

                    # Already a usable format (ogg/mp3)
                    suffix = ".ogg" if "ogg" in mime else (
                        ".mp3" if "mp3" in mime or "mpeg" in mime else ".wav"
                    )
                    tmp = tempfile.NamedTemporaryFile(
                        suffix=suffix, delete=False,
                    )
                    tmp.write(audio_bytes)
                    tmp.close()
                    return Path(tmp.name)

            log.warning("Gemini TTS response had no audio parts")
            return None

        except Exception:
            log.exception("Gemini TTS failed")
            return None


class OpenAITTS:
    """TTS via OpenAI's tts-1 model.

    High quality, simple API. Returns an MP3 file.
    """

    API_URL = "https://api.openai.com/v1/audio/speech"

    def __init__(
        self,
        api_key: str,
        voice: str = "onyx",
        model: str = "tts-1",
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key required for TTS")
        self._api_key = api_key
        self._voice = voice
        self._model = model

        import httpx
        self._client = httpx.AsyncClient(timeout=60.0)
        log.info("OpenAITTS ready: model=%s, voice=%s", self._model, self._voice)

    async def close(self) -> None:
        await self._client.aclose()

    async def synthesize(self, text: str) -> Path | None:
        if len(text) > 4000:
            text = text[:4000] + "... message truncated."

        try:
            resp = await self._client.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "input": text,
                    "voice": self._voice,
                    "response_format": "opus",
                },
            )

            if resp.status_code != 200:
                log.warning("OpenAI TTS error %d: %s", resp.status_code, resp.text[:200])
                return None

            tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
            tmp.write(resp.content)
            tmp.close()

            log.info(
                "OpenAI TTS: %d bytes for %d chars",
                len(resp.content), len(text),
            )
            return Path(tmp.name)

        except Exception:
            log.exception("OpenAI TTS failed")
            return None


def create_tts(voice_config) -> TTSProvider | None:
    """Factory — build the configured TTS provider."""
    provider = voice_config.tts_provider.lower()
    voice = getattr(voice_config, "tts_voice", "Kore")

    try:
        if provider == "openai":
            import os
            key = os.environ.get("OPENAI_API_KEY", "")
            return OpenAITTS(key)
        else:
            # Default: Gemini TTS (reuses the same API key as STT)
            return GeminiTTS(voice_config.stt_api_key, voice=voice)
    except ValueError as e:
        log.warning("TTS provider '%s' unavailable: %s", provider, e)
        return None
