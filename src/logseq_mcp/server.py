from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP, Context

from logseq_mcp.client import LogseqClient


@dataclass
class AppContext:
    client: LogseqClient


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[AppContext]:
    client = LogseqClient()
    async with client:
        yield AppContext(client=client)


mcp = FastMCP("ya-logseq-mcp", lifespan=lifespan)

# Import tools so they register their decorators on `mcp`
from logseq_mcp.tools import core as _core  # noqa: E402, F401
from logseq_mcp.tools import nav as _nav  # noqa: E402, F401

# Comment out this line if you want the MCP Server to be read only
from logseq_mcp.tools import write as _write  # noqa: E402, F401
