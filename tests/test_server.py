import asyncio
import json
import sys
import pytest
import httpx
from unittest.mock import AsyncMock, patch


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")


def test_tool_registered(token_env):
    """Core and write tools must be registered on the FastMCP instance."""
    from logseq_mcp.server import mcp
    # FastMCP stores tools in _tool_manager; access via internal registry
    tool_names = list(mcp._tool_manager._tools.keys())
    expected = {
        "health",
        "get_page",
        "get_block",
        "list_pages",
        "get_references",
        "page_create",
        "block_append",
        "block_update",
        "block_delete",
        "journal_today",
        "journal_append",
        "journal_range",
        "move_block",
        "delete_page",
        "rename_page",
    }
    missing = expected.difference(tool_names)
    assert not missing, f"missing tools {sorted(missing)} from: {tool_names}"


def test_server_branding_name(token_env):
    """Server metadata should expose the canonical branded runtime name."""
    from logseq_mcp.server import mcp

    names = []
    for attr in ("name", "server_name"):
        value = getattr(mcp, attr, None)
        if isinstance(value, str) and value:
            names.append(value)

    lowlevel = getattr(mcp, "_mcp_server", None)
    if lowlevel is not None:
        for attr in ("name", "server_name"):
            value = getattr(lowlevel, attr, None)
            if isinstance(value, str) and value:
                names.append(value)

    assert "lsmcp" in set(names), f"server name metadata mismatch: {sorted(set(names))}"


def test_stderr_only(token_env, capsys):
    """Importing server modules must not write to stdout."""
    # Re-import to trigger any module-level side effects
    import importlib
    import logseq_mcp.server
    importlib.reload(logseq_mcp.server)
    captured = capsys.readouterr()
    assert captured.out == "", f"stdout contaminated: {captured.out!r}"


@pytest.mark.asyncio
async def test_lifespan_closes_client(token_env):
    """httpx.AsyncClient must be closed after lifespan context exits."""
    from logseq_mcp.server import lifespan, mcp, AppContext
    closed_calls = []

    original_aclose = httpx.AsyncClient.aclose

    async def tracking_aclose(self):
        closed_calls.append(True)
        await original_aclose(self)

    with patch.object(httpx.AsyncClient, "aclose", tracking_aclose):
        async with lifespan(mcp) as ctx:
            assert isinstance(ctx, AppContext)
            assert ctx.client._http is not None

    assert len(closed_calls) >= 1, "httpx.AsyncClient.aclose() was not called"


@pytest.mark.asyncio
async def test_health_returns_json(token_env):
    """health tool returns JSON with status, graph, page_count."""
    from logseq_mcp.tools.core import health

    # Mock the client._call to return fake Logseq responses
    async def fake_call(method, *args):
        if method == "logseq.App.getCurrentGraph":
            return {"name": "test-graph", "path": "/graphs/test"}
        if method == "logseq.Editor.getAllPages":
            return [{"name": "page1"}, {"name": "page2"}, {"name": "page3"}]
        return None

    # Build a mock context
    mock_client = AsyncMock()
    mock_client._call = fake_call

    from dataclasses import dataclass
    from logseq_mcp.server import AppContext

    mock_ctx = AsyncMock()
    mock_ctx.request_context.lifespan_context = AppContext(client=mock_client)

    result = await health(mock_ctx)
    data = json.loads(result)

    assert data["status"] == "ok"
    assert data["graph"] == "test-graph"
    assert data["page_count"] == 3


@pytest.mark.asyncio
async def test_health_on_unreachable_logseq(token_env):
    """health tool raises McpError when Logseq is unreachable."""
    from mcp import McpError
    from logseq_mcp.tools.core import health
    from logseq_mcp.server import AppContext

    # Use the real client but patch _call to raise
    mock_client = AsyncMock()
    from mcp.types import ErrorData, INTERNAL_ERROR
    mock_client._call.side_effect = McpError(
        ErrorData(code=INTERNAL_ERROR, message="Logseq is not running or unreachable at http://127.0.0.1:12315")
    )

    mock_ctx = AsyncMock()
    mock_ctx.request_context.lifespan_context = AppContext(client=mock_client)

    with pytest.raises(McpError) as exc_info:
        await health(mock_ctx)

    assert "not running or unreachable" in str(exc_info.value)
