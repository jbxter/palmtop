"""Speech-to-text providers for the agent's voice interface.

Supports:
  - Gemini Flash (cloud, reuses existing GOOGLE_API_KEY — recommended)
  - whisper.cpp (local, runs on the S21 via Termux — offline fallback)
  - OpenAI Whisper API (cloud, requires separate OPENAI_API_KEY)

Telegram voice messages arrive as OGG Opus. Gemini and OpenAI accept
OGG directly. whisper.cpp needs 16 kHz mono WAV, so ffmpeg handles
the conversion for that path only.
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
class STTProvider(Protocol):
    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe an audio file to text. Returns empty string on failure."""
        ...


class GeminiSTT:
    """Transcription via Gemini's native audio understanding.

    Sends the OGG file as inline base64 data to Gemini Flash.
    Reuses the existing GOOGLE_API_KEY — no extra setup needed.
    """

    API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        if not api_key:
            raise ValueError("Google API key required for Gemini STT")
        self._api_key = api_key
        self._model = model

        import httpx
        self._client = httpx.AsyncClient(timeout=60.0)
        log.info("GeminiSTT ready: model=%s", self._model)

    async def close(self) -> None:
        await self._client.aclose()

    async def transcribe(self, audio_path: Path) -> str:
        try:
            audio_bytes = audio_path.read_bytes()
            audio_b64 = base64.standard_b64encode(audio_bytes).decode("ascii")

            body = {
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/ogg",
                                "data": audio_b64,
                            }
                        },
                        {
                            "text": (
                                "Transcribe this audio exactly as spoken. "
                                "Return only the transcription, no commentary, "
                                "no timestamps, no formatting. If the audio is "
                                "silent or unintelligible, return an empty string."
                            ),
                        },
                    ]
                }],
                "generationConfig": {
                    "maxOutputTokens": 2048,
                    "temperature": 0.0,
                },
            }

            url = f"{self.API_URL}/{self._model}:generateContent"
            resp = await self._client.post(
                url,
                headers={
                    "content-type": "application/json",
                    "x-goog-api-key": self._api_key,
                },
                json=body,
            )

            if resp.status_code != 200:
                log.warning("Gemini STT error %d: %s", resp.status_code, resp.text[:200])
                return ""

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                reason = data.get("promptFeedback", {}).get("blockReason", "unknown")
                log.warning("Gemini STT returned no candidates (blocked: %s)", reason)
                return ""

            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return ""

            transcript = parts[0].get("text", "").strip()
            log.info("Transcribed %d chars via Gemini", len(transcript))
            return transcript

        except Exception:
            log.exception("Gemini transcription failed")
            return ""


class FallbackSTT:
    """Tries the primary provider first, falls back to secondary on failure.

    Typical setup: Gemini (cloud) primary, whisper.cpp (local) fallback.
    """

    def __init__(self, primary: STTProvider, fallback: STTProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        log.info(
            "FallbackSTT: %s → %s",
            type(primary).__name__, type(fallback).__name__,
        )

    async def close(self) -> None:
        if hasattr(self._primary, "close"):
            await self._primary.close()
        if hasattr(self._fallback, "close"):
            await self._fallback.close()

    async def transcribe(self, audio_path: Path) -> str:
        result = await self._primary.transcribe(audio_path)
        if result:
            return result
        log.info("Primary STT returned empty, trying fallback")
        return await self._fallback.transcribe(audio_path)


class WhisperCppSTT:
    """Local transcription via the whisper.cpp CLI binary.

    Requires `whisper-cpp` and `ffmpeg` on PATH (Termux: pkg install).
    """

    def __init__(self, model_path: str = "models/ggml-base.en.bin", n_threads: int = 4) -> None:
        self._model = Path(model_path)
        self._threads = n_threads
        self._whisper_bin = shutil.which("whisper-cpp") or shutil.which("main")
        self._ffmpeg_bin = shutil.which("ffmpeg")

        if not self._whisper_bin:
            raise FileNotFoundError(
                "whisper-cpp binary not found on PATH. "
                "Install via: pkg install whisper-cpp (Termux) "
                "or build from https://github.com/ggerganov/whisper.cpp"
            )
        if not self._ffmpeg_bin:
            raise FileNotFoundError(
                "ffmpeg not found on PATH. Install via: pkg install ffmpeg"
            )
        if not self._model.exists():
            raise FileNotFoundError(
                f"Whisper model not found: {self._model}. Download with:\n"
                f"  wget -P models/ https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
            )
        log.info("WhisperCppSTT ready: model=%s, threads=%d", self._model, self._threads)

    async def transcribe(self, audio_path: Path) -> str:
        wav_path = None
        try:
            # Convert OGG → 16 kHz mono WAV
            import os
            wav_fd, wav_str = tempfile.mkstemp(suffix=".wav")
            wav_path = Path(wav_str)
            os.close(wav_fd)

            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg_bin, "-y", "-i", str(audio_path),
                "-ar", "16000", "-ac", "1", "-f", "wav", str(wav_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                log.warning("ffmpeg failed: %s", stderr.decode()[:200])
                return ""

            # Run whisper.cpp
            proc = await asyncio.create_subprocess_exec(
                self._whisper_bin,
                "-m", str(self._model),
                "-f", str(wav_path),
                "-t", str(self._threads),
                "--no-timestamps",
                "-np",  # no progress
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            if proc.returncode != 0:
                log.warning("whisper-cpp failed: %s", stderr.decode()[:200])
                return ""

            transcript = stdout.decode().strip()
            # whisper.cpp sometimes wraps output in [BLANK_AUDIO] or similar
            if "[BLANK_AUDIO]" in transcript or not transcript:
                return ""

            log.info("Transcribed %d chars from %s", len(transcript), audio_path.name)
            return transcript

        except asyncio.TimeoutError:
            log.warning("Transcription timed out for %s", audio_path.name)
            return ""
        except Exception:
            log.exception("Transcription failed for %s", audio_path.name)
            return ""
        finally:
            if wav_path and wav_path.exists():
                wav_path.unlink(missing_ok=True)


class OpenAIWhisperSTT:
    """Cloud transcription via OpenAI's Whisper API.

    Accepts OGG directly — no ffmpeg conversion needed.
    """

    API_URL = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("OpenAI API key required for Whisper cloud STT")
        self._api_key = api_key

        import httpx
        self._client = httpx.AsyncClient(timeout=60.0)
        log.info("OpenAIWhisperSTT ready")

    async def close(self) -> None:
        await self._client.aclose()

    async def transcribe(self, audio_path: Path) -> str:
        try:
            with open(audio_path, "rb") as f:
                resp = await self._client.post(
                    self.API_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": (audio_path.name, f, "audio/ogg")},
                    data={"model": "whisper-1"},
                )

            if resp.status_code != 200:
                log.warning("OpenAI Whisper error %d: %s", resp.status_code, resp.text[:200])
                return ""

            transcript = resp.json().get("text", "").strip()
            log.info("Transcribed %d chars via OpenAI", len(transcript))
            return transcript

        except Exception:
            log.exception("OpenAI transcription failed")
            return ""


def create_stt(voice_config) -> STTProvider | None:
    """Factory — build the configured STT provider with optional fallback.

    Default: GeminiSTT (reuses GOOGLE_API_KEY) with WhisperCppSTT fallback.
    If Gemini key isn't available, tries whisper.cpp alone.
    """
    provider = voice_config.stt_provider.lower()

    try:
        if provider == "openai":
            return OpenAIWhisperSTT(voice_config.stt_api_key)

        if provider == "gemini":
            primary = GeminiSTT(voice_config.stt_api_key)
            # Try to add whisper.cpp as offline fallback
            try:
                fallback = WhisperCppSTT(model_path=voice_config.stt_model_path)
                return FallbackSTT(primary, fallback)
            except FileNotFoundError:
                log.info("whisper.cpp not available — Gemini STT only (no offline fallback)")
                return primary

        if provider == "whisper_cpp":
            return WhisperCppSTT(model_path=voice_config.stt_model_path)

        log.warning("Unknown STT provider '%s', trying Gemini", provider)
        return GeminiSTT(voice_config.stt_api_key)

    except (FileNotFoundError, ValueError) as e:
        log.warning("STT provider '%s' unavailable: %s", provider, e)
        # Last resort: try whatever we can
        for fallback_fn in [
            lambda: GeminiSTT(voice_config.stt_api_key),
            lambda: WhisperCppSTT(model_path=voice_config.stt_model_path),
        ]:
            try:
                return fallback_fn()
            except (FileNotFoundError, ValueError):
                continue
        return None
