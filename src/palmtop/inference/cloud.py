from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from palmtop.inference.base import Message

log = logging.getLogger(__name__)


_PHONE_LIMITS = httpx.Limits(max_connections=5, max_keepalive_connections=2)


class AnthropicBackend:
    """Anthropic Messages API via httpx — no SDK needed, no Rust deps."""

    API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=60.0, limits=_PHONE_LIMITS)

    async def complete(self, messages: list[Message], max_tokens: int = 1024) -> str:
        system = None
        api_messages = []
        for m in messages:
            if m.role == "system":
                system = m.content
            else:
                api_messages.append({"role": m.role, "content": m.content})

        body: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if system:
            body["system"] = system

        resp = await self._client.post(
            self.API_URL,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )

        if resp.status_code != 200:
            log.error("Anthropic API error %d: %s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"Anthropic API returned {resp.status_code}")

        return resp.json()["content"][0]["text"]

    async def stream_complete(
        self, messages: list[Message], max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        """Yield text chunks via Anthropic SSE streaming."""
        system = None
        api_messages = []
        for m in messages:
            if m.role == "system":
                system = m.content
            else:
                api_messages.append({"role": m.role, "content": m.content})

        body: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": api_messages,
            "stream": True,
        }
        if system:
            body["system"] = system

        async with self._client.stream(
            "POST",
            self.API_URL,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        ) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise RuntimeError(f"Anthropic stream error {resp.status_code}: {text[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield text

    async def close(self) -> None:
        await self._client.aclose()


class GeminiBackend:
    """Google Gemini API via httpx."""

    API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=60.0, limits=_PHONE_LIMITS)

    def _build_gemini_request(
        self, messages: list[Message], max_tokens: int
    ) -> tuple[list, dict, str | None]:
        """Parse messages into Gemini format. Returns (contents, gen_config, system)."""
        system = None
        contents = []
        for m in messages:
            if m.role == "system":
                system = m.content
            else:
                role = "model" if m.role == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": m.content}]})
        return contents, {"maxOutputTokens": max_tokens}, system

    def _gemini_headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "x-goog-api-key": self._api_key,
        }

    @staticmethod
    def _extract_gemini_text(data: dict) -> str:
        """Extract text from a Gemini response, handling empty/filtered responses."""
        candidates = data.get("candidates", [])
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "unknown")
            raise RuntimeError(f"Gemini returned no candidates (blocked: {reason})")
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            finish = candidates[0].get("finishReason", "unknown")
            raise RuntimeError(f"Gemini returned empty response (finish: {finish})")
        return parts[0].get("text", "")

    async def complete(self, messages: list[Message], max_tokens: int = 1024) -> str:
        contents, gen_config, system = self._build_gemini_request(messages, max_tokens)

        body: dict = {"contents": contents, "generationConfig": gen_config}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{self.API_URL}/{self._model}:generateContent"
        resp = await self._client.post(url, headers=self._gemini_headers(), json=body)

        if resp.status_code != 200:
            log.error("Gemini API error %d: %s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"Gemini API returned {resp.status_code}")

        return self._extract_gemini_text(resp.json())

    async def stream_complete(
        self, messages: list[Message], max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        """Yield text chunks via Gemini SSE streaming."""
        contents, gen_config, system = self._build_gemini_request(messages, max_tokens)

        body: dict = {"contents": contents, "generationConfig": gen_config}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{self.API_URL}/{self._model}:streamGenerateContent?alt=sse"
        async with self._client.stream(
            "POST",
            url,
            headers=self._gemini_headers(),
            json=body,
        ) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise RuntimeError(f"Gemini stream error {resp.status_code}: {text[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                candidates = event.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    for part in parts:
                        text = part.get("text", "")
                        if text:
                            yield text

    async def close(self) -> None:
        await self._client.aclose()


def create_cloud_backend(provider: str, api_key: str, model: str | None = None):
    if provider == "anthropic":
        return AnthropicBackend(api_key, model=model or "claude-haiku-4-5-20251001")
    elif provider == "google":
        return GeminiBackend(api_key, model=model or "gemini-2.5-flash")
    else:
        raise ValueError(f"Unknown cloud provider: {provider}")
