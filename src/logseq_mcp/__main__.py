import argparse
import logging
import os
import sys

_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _build_parser(prog: str = "ya-logseq-mcp") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Python MCP server for Logseq.",
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
    prog = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "ya-logseq-mcp"
    args = _build_parser(prog=prog).parse_args()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

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
