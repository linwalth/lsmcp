"""Tests for __main__ transport/host/port resolution and token cache flows."""

import os

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


def test_login_and_logout_are_mutex():
    from logseq_mcp.__main__ import _build_parser
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--login", "--logout"])


# ---------------------------------------------------------------------------
# token cache helpers
# ---------------------------------------------------------------------------

def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from logseq_mcp.__main__ import _cache_token, _load_cached_token, _token_cache_path
    path = _cache_token("sekret-abc")
    assert path == tmp_path / "lsmcp" / "token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert _load_cached_token() == "sekret-abc"


def test_load_cached_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from logseq_mcp.__main__ import _load_cached_token
    assert _load_cached_token() is None


def test_clear_cached_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from logseq_mcp.__main__ import _cache_token, _clear_cached_token, _token_cache_path
    _cache_token("will-be-gone")
    assert _clear_cached_token() is True
    assert not _token_cache_path().exists()


def test_clear_cached_no_op_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from logseq_mcp.__main__ import _clear_cached_token
    assert _clear_cached_token() is False


# ---------------------------------------------------------------------------
# main() login/logout flows
# ---------------------------------------------------------------------------

def test_login_flow_caches_then_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["lsmcp", "--login"])
    monkeypatch.setattr("builtins.input", lambda *_: "tok-xyz")
    from logseq_mcp.__main__ import main, _load_cached_token
    main()
    assert _load_cached_token() == "tok-xyz"
    err = capsys.readouterr().err
    assert "Cached token" in err


def test_login_rejects_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["lsmcp", "--login"])
    monkeypatch.setattr("builtins.input", lambda *_: "")
    from logseq_mcp.__main__ import main
    with pytest.raises(SystemExit) as exc:
        main()
    assert "empty" in str(exc.value)


def test_logout_removes_existing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["lsmcp", "--logout"])
    from logseq_mcp.__main__ import _cache_token, main, _token_cache_path
    _cache_token("bye")
    main()
    assert not _token_cache_path().exists()
    assert "removed" in capsys.readouterr().err


def test_logout_noop_when_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["lsmcp", "--logout"])
    from logseq_mcp.__main__ import main
    main()
    assert "nothing to remove" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() token resolution precedence
# ---------------------------------------------------------------------------

def test_auto_uses_cached_token_when_no_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["lsmcp"])
    monkeypatch.delenv("LOGSEQ_API_TOKEN", raising=False)
    from logseq_mcp.__main__ import _cache_token, main
    _cache_token("from-cache")
    import logseq_mcp.server as srv
    ran = {}

    class _Stub:
        settings = type("s", (), {"host": "", "port": 0})()

        def run(self, transport=None):
            ran["transport"] = transport

    monkeypatch.setattr(srv, "mcp", _Stub())
    main()
    assert os.environ.get("LOGSEQ_API_TOKEN") == "from-cache"
    assert ran["transport"] == "stdio"


def test_env_token_takes_precedence_over_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["lsmcp"])
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "from-env")
    from logseq_mcp.__main__ import _cache_token, main
    _cache_token("from-cache")
    import logseq_mcp.server as srv

    class _Stub:
        settings = type("s", (), {"host": "", "port": 0})()

        def run(self, transport=None):
            pass

    monkeypatch.setattr(srv, "mcp", _Stub())
    main()
    assert os.environ.get("LOGSEQ_API_TOKEN") == "from-env"


def test_no_token_anywhere_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["lsmcp"])
    monkeypatch.delenv("LOGSEQ_API_TOKEN", raising=False)
    from logseq_mcp.__main__ import main
    with pytest.raises(SystemExit) as exc:
        main()
    assert "LOGSEQ_API_TOKEN" in str(exc.value)
