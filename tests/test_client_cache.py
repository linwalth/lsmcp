"""Tests for LogseqClient TTL cache + write invalidation + pool."""

import httpx
import pytest


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")


async def test_cache_hit_avoids_second_call(token_env):
    from logseq_mcp.client import LogseqClient

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"result": "ok"})

    client = LogseqClient()
    async with client:
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
        r1 = await client._call("logseq.Editor.getAllPages")
        r2 = await client._call("logseq.Editor.getAllPages")

    assert r1 == {"result": "ok"}
    assert r2 == {"result": "ok"}
    assert len(calls) == 1  # second served from cache


async def test_write_invalidates_cache(token_env):
    from logseq_mcp.client import LogseqClient

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"result": "ok"})

    client = LogseqClient()
    async with client:
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
        await client._call("logseq.Editor.getAllPages")
        await client._call("logseq.Editor.getAllPages")  # cached, no call
        await client._call("logseq.Editor.appendBlockInPage", "page", "content")
        await client._call("logseq.Editor.getAllPages")  # cache flushed -> new call

    # 3rd call was the write, then getAllPages fetched again => 3 total HTTP posts
    assert len(calls) == 3


async def test_non_cacheable_not_cached(token_env):
    from logseq_mcp.client import LogseqClient

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"result": "ok"})

    client = LogseqClient()
    async with client:
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
        await client._call("logseq.Editor.someUncachedThing")
        await client._call("logseq.Editor.someUncachedThing")

    assert len(calls) == 2


async def test_cache_disabled_when_ttl_zero(token_env, monkeypatch):
    monkeypatch.setenv("LOGSEQ_CACHE_TTL_SECONDS", "0")
    from logseq_mcp.client import LogseqClient

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"result": "ok"})

    client = LogseqClient()
    assert client._cache_ttl == 0.0
    async with client:
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
        await client._call("logseq.Editor.getAllPages")
        await client._call("logseq.Editor.getAllPages")

    assert len(calls) == 2
