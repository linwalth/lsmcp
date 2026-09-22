import asyncio
import os
import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")


def make_transport(responses: list) -> httpx.MockTransport:
    """Return a MockTransport that serves responses in order."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        resp = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        if isinstance(resp, Exception):
            raise resp
        return resp

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_auth_header(token_env):
    from logseq_mcp.client import LogseqClient
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"result": "ok"})

    client = LogseqClient()
    async with client:
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
        await client._call("logseq.App.getCurrentGraph")

    assert len(captured) == 1
    assert captured[0] == "Bearer test-token"


@pytest.mark.asyncio
async def test_retry_on_transport_error(token_env):
    from logseq_mcp.client import LogseqClient
    from mcp import McpError
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"result": "ok"})

    client = LogseqClient()
    # Patch sleep to avoid waiting
    with patch("asyncio.sleep", new_callable=AsyncMock):
        async with client:
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
            result = await client._call("logseq.App.getCurrentGraph")

    assert result == {"result": "ok"}
    assert call_count == 3  # failed twice, succeeded on third


@pytest.mark.asyncio
async def test_retry_on_5xx(token_env):
    from logseq_mcp.client import LogseqClient
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"result": "ok"})

    client = LogseqClient()
    with patch("asyncio.sleep", new_callable=AsyncMock):
        async with client:
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
            result = await client._call("logseq.App.getCurrentGraph")

    assert result == {"result": "ok"}
    assert call_count == 3


@pytest.mark.asyncio
async def test_no_retry_on_4xx(token_env):
    from logseq_mcp.client import LogseqClient
    from mcp import McpError
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, text="unauthorized")

    client = LogseqClient()
    async with client:
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
        with pytest.raises(McpError) as exc_info:
            await client._call("logseq.App.getCurrentGraph")

    assert call_count == 1  # no retry
    assert "401" in str(exc_info.value)


@pytest.mark.asyncio
async def test_connect_error_message(token_env):
    from logseq_mcp.client import LogseqClient
    from mcp import McpError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = LogseqClient()
    with patch("asyncio.sleep", new_callable=AsyncMock):
        async with client:
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
            with pytest.raises(McpError) as exc_info:
                await client._call("logseq.App.getCurrentGraph")

    assert "not running or unreachable" in str(exc_info.value)


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("LOGSEQ_API_TOKEN", raising=False)
    from logseq_mcp.client import LogseqClient
    with pytest.raises(RuntimeError, match="LOGSEQ_API_TOKEN"):
        LogseqClient()


@pytest.mark.asyncio
async def test_calls_overlap_under_bounded_concurrency(token_env):
    """Two concurrent _call() invocations overlap because the semaphore caps
    concurrency at 4 (chosen so journal_range can fan out), not serialize at 1.
    With room for both, each transport starts before either finishes:
    ['start', 'start', 'end', 'end']."""
    from logseq_mcp.client import LogseqClient
    order = []

    # Use a real async transport
    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            order.append("start")
            await asyncio.sleep(0.01)
            order.append("end")
            return httpx.Response(200, json={"result": "ok"})

    client = LogseqClient()
    async with client:
        client._http = httpx.AsyncClient(transport=SlowTransport(), timeout=10.0)
        await asyncio.gather(
            client._call("method.a"),
            client._call("method.b"),
        )

    # semaphore(4): start,start,end,end (overlapping, not sequential)
    assert order == ["start", "start", "end", "end"]
