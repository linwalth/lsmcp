import argparse
import logging
import os
import stat
import sys
from pathlib import Path

_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _token_cache_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(base) / "lsmcp" / "token"


def _cache_token(token: str) -> Path:
    path = _token_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    return path


def _clear_cached_token() -> bool:
    path = _token_cache_path()
    if path.exists():
        path.unlink()
        return True
    return False


def _load_cached_token() -> str | None:
    path = _token_cache_path()
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _build_parser(prog: str = "lsmcp") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Python MCP server for Logseq.",
    )
    token_group = parser.add_mutually_exclusive_group()
    token_group.add_argument(
        "--login",
        action="store_true",
        help="Prompt for the Logseq API token and cache it to ~/.config/lsmcp/token (0600), then exit.",
    )
    token_group.add_argument(
        "--logout",
        action="store_true",
        help="Forget the cached Logseq API token (delete the cache file), then exit.",
    )
    parser.add_argument(
        "--transport",
        choices=_TRANSPORTS + ("http",),
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help=(
            "MCP transport (default: stdio, or MCP_TRANSPORT env). "
            "'http' is an alias for 'streamable-http'."
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "127.0.0.1"),
        help="Bind host for http/sse transports (default: 127.0.0.1, or MCP_HOST env).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8765")),
        help="Bind port for http/sse transports (default: 8765, or MCP_PORT env).",
    )
    return parser


def main() -> None:
    prog = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "lsmcp"
    args = _build_parser(prog=prog).parse_args()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.login:
        token = input("Paste your Logseq API token: ").strip()
        if not token:
            raise SystemExit("token must not be empty")
        path = _cache_token(token)
        print(f"Cached token to {path}", file=sys.stderr)
        return

    if args.logout:
        removed = _clear_cached_token()
        action = "removed" if removed else "nothing to remove (no cached token)"
        print(action, file=sys.stderr)
        return

    # Resolve token: explicit env wins, then fall back to the cache.
    token = os.environ.get("LOGSEQ_API_TOKEN") or _load_cached_token()
    if not token:
        raise SystemExit(
            "LOGSEQ_API_TOKEN is required. Set it in your environment, "
            "run 'lsmcp --login' to cache it, or set LOGSEQ_API_TOKEN."
        )
    os.environ["LOGSEQ_API_TOKEN"] = token

    transport = "streamable-http" if args.transport == "http" else args.transport
    if transport not in _TRANSPORTS:
        raise SystemExit(f"unknown transport: {args.transport}")

    from logseq_mcp.server import mcp

    # host/port only matter for non-stdio transports, but setting them is harmless.
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
