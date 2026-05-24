from __future__ import annotations

import logging
import re
from urllib.parse import unquote

import httpx

from pocket_agent.tools.base import Tool

log = logging.getLogger(__name__)

MAX_RESULTS = 5
MIN_GOOD_RESULTS = 2  # if fewer than this, try the next provider


class WebSearchTool(Tool):
    """Web search with provider fallback chain and key rotation.

    Accepts multiple API keys per provider. On each search:
    1. Try providers in order, rotating keys within each provider
    2. If results are empty/poor, try the next provider
    3. If rate-limited (429) or error, rotate to next key or next provider
    4. DuckDuckGo is always the final fallback (no key needed)
    """

    name = "search"
    description = "Search the web for current information. Usage: [TOOL:search] your query"

    def __init__(
        self,
        brave_keys: list[str] | None = None,
        serper_keys: list[str] | None = None,
        preferred_order: list[str] | None = None,
    ) -> None:
        self._client: httpx.AsyncClient | None = None

        # Key pools — rotate through them
        self._brave_keys = [k for k in (brave_keys or []) if k]
        self._serper_keys = [k for k in (serper_keys or []) if k]
        self._brave_idx = 0
        self._serper_idx = 0

        # Build provider chain based on what's available
        if preferred_order:
            self._chain = [p.lower() for p in preferred_order]
        else:
            self._chain = []
            if self._serper_keys:
                self._chain.append("serper")
            if self._brave_keys:
                self._chain.append("brave")

        # DDG is always the final fallback
        if "duckduckgo" not in self._chain:
            self._chain.append("duckduckgo")

        log.info(
            "Search chain: %s (brave keys: %d, serper keys: %d)",
            " → ".join(self._chain), len(self._brave_keys), len(self._serper_keys),
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PocketAgent/1.0)"},
                follow_redirects=True,
            )
        return self._client

    def _next_brave_key(self) -> str | None:
        if not self._brave_keys:
            return None
        key = self._brave_keys[self._brave_idx % len(self._brave_keys)]
        self._brave_idx += 1
        return key

    def _next_serper_key(self) -> str | None:
        if not self._serper_keys:
            return None
        key = self._serper_keys[self._serper_idx % len(self._serper_keys)]
        self._serper_idx += 1
        return key

    async def run(self, query: str) -> str:
        last_error = ""
        for provider in self._chain:
            try:
                if provider == "brave":
                    result = await self._try_brave(query)
                elif provider == "serper":
                    result = await self._try_serper(query)
                else:
                    result = await self._search_ddg(query)

                if result and _is_good_result(result):
                    return result

                log.info("Weak results from %s, trying next provider", provider)

            except _RateLimited as e:
                log.warning("%s rate-limited: %s — trying next", provider, e)
                last_error = str(e)
            except Exception as e:
                log.warning("%s failed: %s — trying next", provider, e)
                last_error = str(e)

        return last_error or "No results found across all search providers."

    # ── Brave Search ─────────────────────────────────────────────

    async def _try_brave(self, query: str) -> str:
        """Try all brave keys until one works."""
        if not self._brave_keys:
            return ""
        attempts = len(self._brave_keys)
        for _ in range(attempts):
            key = self._next_brave_key()
            result = await self._search_brave(query, key)
            if result:
                return result
        return ""

    async def _search_brave(self, query: str, api_key: str) -> str:
        client = self._get_client()
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": MAX_RESULTS},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        )
        if resp.status_code == 429:
            raise _RateLimited(f"Brave key ...{api_key[-4:]}")
        if resp.status_code != 200:
            log.warning("Brave HTTP %d with key ...%s", resp.status_code, api_key[-4:])
            return ""

        data = resp.json()
        results = []
        for item in (data.get("web", {}).get("results", []))[:MAX_RESULTS]:
            title = item.get("title", "")
            snippet = item.get("description", "")
            url = item.get("url", "")
            if title:
                results.append(f"• {title}\n  {snippet}\n  {url}")

        return "\n\n".join(results) if results else ""

    # ── Serper (Google) ──────────────────────────────────────────

    async def _try_serper(self, query: str) -> str:
        """Try all serper keys until one works."""
        if not self._serper_keys:
            return ""
        attempts = len(self._serper_keys)
        for _ in range(attempts):
            key = self._next_serper_key()
            result = await self._search_serper(query, key)
            if result:
                return result
        return ""

    async def _search_serper(self, query: str, api_key: str) -> str:
        client = self._get_client()
        resp = await client.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": MAX_RESULTS},
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        )
        if resp.status_code == 429:
            raise _RateLimited(f"Serper key ...{api_key[-4:]}")
        if resp.status_code != 200:
            log.warning("Serper HTTP %d with key ...%s", resp.status_code, api_key[-4:])
            return ""

        data = resp.json()
        results = []

        # Knowledge graph
        kg = data.get("knowledgeGraph", {})
        if kg.get("description"):
            results.append(f"• {kg.get('title', '')}: {kg['description']}")

        for item in data.get("organic", [])[:MAX_RESULTS]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            url = item.get("link", "")
            if title:
                results.append(f"• {title}\n  {snippet}\n  {url}")

        return "\n\n".join(results) if results else ""

    # ── DuckDuckGo (always available) ────────────────────────────

    async def _search_ddg(self, query: str) -> str:
        client = self._get_client()
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "b": ""},
        )
        if resp.status_code != 200:
            return f"Search failed (HTTP {resp.status_code})"
        return _parse_ddg_html(resp.text)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


class _RateLimited(Exception):
    pass


def _is_good_result(text: str) -> bool:
    """Check if search results are substantive enough."""
    if not text or text.startswith("No results") or text.startswith("Search failed"):
        return False
    # Count actual result bullets
    return text.count("•") >= MIN_GOOD_RESULTS


# ── DDG HTML parsing ─────────────────────────────────────────────

def _parse_ddg_html(html: str) -> str:
    results = []
    snippets = re.findall(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</(?:td|span)>',
        html,
        re.DOTALL,
    )
    for url, title, snippet in snippets[:MAX_RESULTS]:
        title = _strip_html(title).strip()
        snippet = _strip_html(snippet).strip()
        url = _extract_url(url)
        if title and snippet:
            results.append(f"• {title}\n  {snippet}\n  {url}")

    if not results:
        zci = re.search(r'class="zci__result"[^>]*>(.*?)</div>', html, re.DOTALL)
        if zci:
            return "Instant answer: " + _strip_html(zci.group(1)).strip()
        return "No results found."

    return "\n\n".join(results)


def _strip_html(text: str) -> str:
    return (
        re.sub(r"<[^>]+>", "", text)
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#x27;", "'")
        .replace("&quot;", '"')
    )


def _extract_url(href: str) -> str:
    match = re.search(r"uddg=([^&]+)", href)
    if match:
        return unquote(match.group(1))
    return href
