from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from palmtop.config.settings import http_limits, resolve_profile
from palmtop.inference.base import Message

log = logging.getLogger(__name__)


def _default_limits() -> httpx.Limits:
    return http_limits(resolve_profile("phone", 1, None))


class AnthropicBackend:
    """Anthropic Messages API via httpx — no SDK needed, no Rust deps."""

    API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        *,
        limits: httpx.Limits | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=60.0, limits=limits or _default_limits())

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

    async def stream_complete(self, messages: list[Message], max_tokens: int = 1024) -> AsyncIterator[str]:
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

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        *,
        limits: httpx.Limits | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=60.0, limits=limits or _default_limits())

    def _build_gemini_request(self, messages: list[Message], max_tokens: int) -> tuple[list, dict, str | None]:
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

    async def stream_complete(self, messages: list[Message], max_tokens: int = 1024) -> AsyncIterator[str]:
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


class OpenAICompatibleBackend:
    """OpenAI-compatible chat completions API.

    Works with: OpenAI, Groq, Together, Fireworks, DeepSeek, Mistral, OpenRouter.
    All use the same request/response format with different base URLs.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        extra_headers: dict[str, str] | None = None,
        *,
        limits: httpx.Limits | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._extra_headers = extra_headers or {}
        self._client = httpx.AsyncClient(timeout=120.0, limits=limits or _default_limits())

    def _headers(self) -> dict[str, str]:
        h = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        h.update(self._extra_headers)
        return h

    async def complete(self, messages: list[Message], max_tokens: int = 1024) -> str:
        api_messages = []
        for m in messages:
            api_messages.append({"role": m.role, "content": m.content})

        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }

        resp = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=body,
        )

        if resp.status_code != 200:
            log.error("%s API error %d: %s", self._model, resp.status_code, resp.text[:200])
            raise RuntimeError(f"OpenAI-compatible API returned {resp.status_code}")

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("OpenAI-compatible API returned no choices")
        return choices[0]["message"]["content"]

    async def stream_complete(self, messages: list[Message], max_tokens: int = 1024) -> AsyncIterator[str]:
        """Yield text chunks via SSE streaming."""
        api_messages = []
        for m in messages:
            api_messages.append({"role": m.role, "content": m.content})

        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": api_messages,
            "stream": True,
        }

        async with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=body,
        ) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise RuntimeError(f"OpenAI-compatible stream error {resp.status_code}: {text[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        yield text

    async def close(self) -> None:
        await self._client.aclose()


class OllamaBackend:
    """Ollama local inference via its OpenAI-compatible API.

    No API key needed. Connects to localhost:11434 by default.
    Great alternative to llama.cpp — no compilation required.
    """

    def __init__(
        self,
        model: str = "llama3.2",
        base_url: str = "http://localhost:11434",
        *,
        limits: httpx.Limits | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=300.0, limits=limits or _default_limits())

    async def complete(self, messages: list[Message], max_tokens: int = 1024) -> str:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        body = {
            "model": self._model,
            "messages": api_messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }

        resp = await self._client.post(
            f"{self._base_url}/api/chat",
            headers={"content-type": "application/json"},
            json=body,
        )

        if resp.status_code != 200:
            log.error("Ollama API error %d: %s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"Ollama API returned {resp.status_code}")

        data = resp.json()
        return data.get("message", {}).get("content", "")

    async def stream_complete(self, messages: list[Message], max_tokens: int = 1024) -> AsyncIterator[str]:
        """Yield text chunks from Ollama streaming."""
        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        body = {
            "model": self._model,
            "messages": api_messages,
            "stream": True,
            "options": {"num_predict": max_tokens},
        }

        async with self._client.stream(
            "POST",
            f"{self._base_url}/api/chat",
            headers={"content-type": "application/json"},
            json=body,
        ) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise RuntimeError(f"Ollama stream error {resp.status_code}: {text[:200]}")
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = event.get("message", {}).get("content", "")
                if content:
                    yield content
                if event.get("done"):
                    break

    async def close(self) -> None:
        await self._client.aclose()


class CohereBackend:
    """Cohere Chat API (v2)."""

    API_URL = "https://api.cohere.com/v2/chat"

    def __init__(
        self,
        api_key: str,
        model: str = "command-r-plus",
        *,
        limits: httpx.Limits | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=120.0, limits=limits or _default_limits())

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

    async def complete(self, messages: list[Message], max_tokens: int = 1024) -> str:
        # Cohere v2 uses the same messages format as OpenAI
        api_messages = []
        for m in messages:
            api_messages.append({"role": m.role, "content": m.content})

        body = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": max_tokens,
        }

        resp = await self._client.post(self.API_URL, headers=self._headers(), json=body)

        if resp.status_code != 200:
            log.error("Cohere API error %d: %s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"Cohere API returned {resp.status_code}")

        data = resp.json()
        # v2 response: {"message": {"content": [{"text": "..."}]}}
        message = data.get("message", {})
        content = message.get("content", [])
        if content and isinstance(content, list):
            return content[0].get("text", "")
        return ""

    async def stream_complete(self, messages: list[Message], max_tokens: int = 1024) -> AsyncIterator[str]:
        """Yield text chunks via Cohere SSE streaming."""
        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        body = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with self._client.stream(
            "POST",
            self.API_URL,
            headers=self._headers(),
            json=body,
        ) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise RuntimeError(f"Cohere stream error {resp.status_code}: {text[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "content-delta":
                    delta = event.get("delta", {}).get("message", {})
                    content = delta.get("content", {})
                    text = content.get("text", "")
                    if text:
                        yield text

    async def close(self) -> None:
        await self._client.aclose()


# ── Provider registry ────────────────────────────────────────────────────

# Default models per provider (used when config doesn't specify a model)
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "anthropic": {"model": "claude-haiku-4-5-20251001"},
    "google": {"model": "gemini-2.5-flash"},
    "openai": {"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"},
    "groq": {"model": "llama-3.3-70b-versatile", "base_url": "https://api.groq.com/openai/v1"},
    "together": {"model": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo", "base_url": "https://api.together.xyz/v1"},
    "fireworks": {
        "model": "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "base_url": "https://api.fireworks.ai/inference/v1",
    },
    "deepseek": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
    "mistral": {"model": "mistral-large-latest", "base_url": "https://api.mistral.ai/v1"},
    "openrouter": {"model": "anthropic/claude-sonnet-4", "base_url": "https://openrouter.ai/api/v1"},
    "ollama": {"model": "llama3.2", "base_url": "http://localhost:11434"},
    "cohere": {"model": "command-r-plus"},
}

# Env var names per provider
PROVIDER_ENV_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "cohere": "COHERE_API_KEY",
}

# Providers that use the OpenAI-compatible format
_OPENAI_COMPATIBLE = {"openai", "groq", "together", "fireworks", "deepseek", "mistral", "openrouter"}


def create_cloud_backend(
    provider: str,
    api_key: str,
    model: str | None = None,
    base_url: str | None = None,
    *,
    limits: httpx.Limits | None = None,
):
    """Create an inference backend for any supported provider.

    Args:
        provider: Provider name (anthropic, google, openai, groq, together,
                  fireworks, deepseek, mistral, openrouter, ollama, cohere).
        api_key: API key (not needed for ollama).
        model: Model name override (uses provider default if None).
        base_url: Base URL override (for custom deployments or Ollama host).
        limits: HTTP pool from the hardware profile. Phone caps when omitted.
    """
    defaults = PROVIDER_DEFAULTS.get(provider)
    if not defaults:
        raise ValueError(
            f"Unknown cloud provider: '{provider}'. Supported: {', '.join(sorted(PROVIDER_DEFAULTS.keys()))}"
        )

    resolved_model = model or defaults["model"]

    if provider == "anthropic":
        return AnthropicBackend(api_key, model=resolved_model, limits=limits)

    if provider == "google":
        return GeminiBackend(api_key, model=resolved_model, limits=limits)

    if provider == "ollama":
        resolved_url = base_url or defaults.get("base_url", "http://localhost:11434")
        return OllamaBackend(model=resolved_model, base_url=resolved_url, limits=limits)

    if provider == "cohere":
        return CohereBackend(api_key, model=resolved_model, limits=limits)

    if provider in _OPENAI_COMPATIBLE:
        resolved_url = base_url or defaults.get("base_url", "https://api.openai.com/v1")
        extra_headers = {}
        # OpenRouter recommends sending site info
        if provider == "openrouter":
            extra_headers["HTTP-Referer"] = "https://github.com/jbxter/palmtop"
            extra_headers["X-Title"] = "Palmtop"
        return OpenAICompatibleBackend(
            api_key,
            model=resolved_model,
            base_url=resolved_url,
            extra_headers=extra_headers,
            limits=limits,
        )

    raise ValueError(f"Unknown cloud provider: '{provider}'")
