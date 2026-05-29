"""Tests for hermes/client.py — the thin Hermes Agent HTTP client."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from palmtop.hermes.client import HermesAPIError, HermesClient


class TestClientBasics:
    def test_base_url_trailing_slash_stripped(self):
        c = HermesClient(base_url="http://localhost:8080/")
        assert c._base_url == "http://localhost:8080"

    def test_headers_without_key(self):
        c = HermesClient()
        h = c._headers()
        assert h["content-type"] == "application/json"
        assert "authorization" not in h

    def test_headers_with_key(self):
        c = HermesClient(api_key="hk-test")
        assert c._headers()["authorization"] == "Bearer hk-test"


class TestHealth:
    @pytest.fixture
    def client(self):
        return HermesClient()

    @pytest.mark.asyncio
    async def test_health_true_on_200(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=httpx.Response(200, json={"ok": True}))
        assert await client.health() is True

    @pytest.mark.asyncio
    async def test_health_false_on_error_status(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=httpx.Response(503, text="down"))
        assert await client.health() is False

    @pytest.mark.asyncio
    async def test_health_false_on_connection_error(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        assert await client.health() is False


class TestMemory:
    @pytest.fixture
    def client(self):
        return HermesClient(api_key="hk-test")

    @pytest.mark.asyncio
    async def test_list_memories_wrapped(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=httpx.Response(200, json={"memories": [{"id": "1"}, {"id": "2"}]}))
        out = await client.list_memories()
        assert [m["id"] for m in out] == ["1", "2"]

    @pytest.mark.asyncio
    async def test_list_memories_bare_list(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=httpx.Response(200, json=[{"id": "1"}]))
        out = await client.list_memories()
        assert out == [{"id": "1"}]

    @pytest.mark.asyncio
    async def test_list_memories_since_passed_as_param(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=httpx.Response(200, json={"memories": []}))
        await client.list_memories(since="cursor-42", limit=10)
        params = client._client.get.call_args.kwargs["params"]
        assert params["since"] == "cursor-42"
        assert params["limit"] == 10

    @pytest.mark.asyncio
    async def test_upsert_returns_stored_record(self, client):
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=httpx.Response(200, json={"id": "abc", "content": "hi"}))
        rec = await client.upsert_memory({"content": "hi"})
        assert rec["id"] == "abc"

    @pytest.mark.asyncio
    async def test_delete_uses_id_in_url(self, client):
        client._client = AsyncMock()
        client._client.delete = AsyncMock(return_value=httpx.Response(204))
        await client.delete_memory("abc")
        url = client._client.delete.call_args.args[0]
        assert url.endswith("/memories/abc")

    @pytest.mark.asyncio
    async def test_error_status_raises(self, client):
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(HermesAPIError, match="500"):
            await client.upsert_memory({"content": "x"})


class TestSkills:
    @pytest.fixture
    def client(self):
        return HermesClient()

    @pytest.mark.asyncio
    async def test_list_skills_wrapped(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=httpx.Response(200, json={"skills": [{"id": "s1"}]}))
        out = await client.list_skills()
        assert out[0]["id"] == "s1"

    @pytest.mark.asyncio
    async def test_get_skill(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=httpx.Response(200, json={"id": "s1", "name": "summarize"}))
        skill = await client.get_skill("s1")
        assert skill["name"] == "summarize"

    @pytest.mark.asyncio
    async def test_get_skill_not_found_raises(self, client):
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=httpx.Response(404, text="nope"))
        with pytest.raises(HermesAPIError, match="404"):
            await client.get_skill("missing")
