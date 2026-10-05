from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ToolAnnotations

from logseq_mcp.client import LogseqClient


@dataclass
class AppContext:
    client: LogseqClient


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[AppContext]:
    client = LogseqClient()
    async with client:
        yield AppContext(client=client)


mcp = FastMCP("lsmcp", lifespan=lifespan)

# Reusable ToolAnnotations profiles for the registered tools. These are HINTS
# for MCP clients (read-only vs destructive vs idempotent), not security
# guarantees. Defined here so tool modules can share them without duplicating
# the literal dicts across files.
ANNOT_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
ANNOT_APPEND = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
ANNOT_UPDATE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
ANNOT_DELETE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)
ANNOT_RENAME = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

# Import tools so they register their decorators on `mcp`
from logseq_mcp.tools import core as _core  # noqa: E402, F401
from logseq_mcp.tools import nav as _nav  # noqa: E402, F401

# Comment out this line if you want the MCP Server to be read only
from logseq_mcp.tools import write as _write  # noqa: E402, F401
