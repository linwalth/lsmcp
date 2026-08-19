import logging
import sys


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from logseq_mcp.server import mcp
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = 8765
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
