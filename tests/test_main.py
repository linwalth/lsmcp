"""Tests for __main__ transport/host/port resolution."""

import pytest


def test_default_transport_is_stdio(monkeypatch):
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("MCP_HOST", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)
    from logseq_mcp.__main__ import _build_parser
    ns = _build_parser().parse_args([])
    assert ns.transport == "stdio"
    assert ns.host == "127.0.0.1"
    assert ns.port == 8765


def test_env_overrides_transport_host_port(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9000")
    from logseq_mcp.__main__ import _build_parser
    ns = _build_parser().parse_args([])
    assert ns.transport == "streamable-http"
    assert ns.host == "0.0.0.0"
    assert ns.port == 9000


def test_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    from logseq_mcp.__main__ import _build_parser
    ns = _build_parser().parse_args(["--transport", "http", "--port", "1234"])
    assert ns.transport == "http"
    assert ns.port == 1234


def test_help_exits_cleanly():
    from logseq_mcp.__main__ import _build_parser
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["--help"])
    assert exc.value.code == 0
