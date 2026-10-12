# lsmcp

<p align="center"><img src="icon.svg" width="64" alt="lsmcp logo"></p>

Python MCP server for Logseq.

<p align="center"><img src="./demo.svg" width="720" alt="lsmcp architecture"></p>

## What This Is

`lsmcp` exposes Logseq read/write tools over MCP so clients like Claude Desktop can automate page and block workflows. It deduplicates blocks by UUID at read time and returns lean responses by default.

## Tools

| Tool | Description |
|------|-------------|
| `health` | Ping Logseq and return graph name and page count |
| `get_page` | Return a page entity and deduplicated block tree by page name |
| `get_block` | Get a single block by UUID |
| `list_pages` | List pages with optional namespace filter (truncated by limit); `slim=True` drops properties |
| `search_pages` | Find pages by a name fragment; returns full paths for nested/namespace pages; `slim=True` drops properties |
| `list_namespace` | List every page beneath a namespace without truncation |
| `list_namespace_tree` | Browse a namespace as a hierarchical tree (parents with nested children) |
| `get_references` | Get backlinks to a page (pages that reference this page) |
| `forward_links` | List the pages that THIS page links to (outbound wikilinks) |
| `graph_stats` | Whole-graph orientation snapshot: page counts, top namespaces (call first) |
| `page_outline` | Flat truncated outline skeleton of a page (progressive disclosure, lighter than `get_page`) |
| `expand_references` | Expand the backlink neighborhood around a page by up to N BFS hops |
| `cross_reference` | Pages that link to BOTH of two pages (intersection of their backlinks) |
| `namespace_stats` | Granular stats for one namespace subtree: depth distribution, child namespaces |
| `search_blocks` | Full-text search over BLOCK CONTENTS (find lore by substance, not just page names) |
| `list_blueprints` | List page-structure templates under the `blaupausen/` namespace |
| `get_blueprint` | Fetch the headed-block layout of a blueprint so new pages match graph conventions |
| `get_conventions` | Structured JSON of all writing conventions (typography, structure, links, statblock schema) |
| `get_namespace_map` | Namespace architecture map (static purpose + live page counts) |
| `validate_page` | Lint a page against conventions: dead links, em-dashes, CJK, bullet prefixes, title redundancy |
| `orphan_report` | Report pages with zero backlinks, classified by orphan policy |
| `similar_pages` | Find near-duplicate page names via fuzzy string matching |
| `query` | Run a Datalog query against Logseq's indexed DB; inline values via `%1`,`%2` placeholders |
| `page_create` | Create a new page with optional properties and initial blocks |
| `block_append` | Append blocks to a page; accepts flat strings or nested objects with content, properties, and children |
| `block_prepend` | Prepend blocks to the top of a page (above existing content) |
| `block_update` | Update block content by UUID |
| `block_delete` | Delete a block by UUID |
| `delete_page` | Delete a page by name |
| `rename_page` | Rename a page from old_name to new_name |
| `move_block` | Move a block before, after, or as a child of a target block |
| `journal_today` | Get or create today's journal page and return its block tree |
| `journal_append` | Append blocks to a journal page for a given date |
| `journal_range` | Return journal entries for all existing pages between start_date and end_date |

## Requirements

- [ ] Python 3.12+ is installed: `python3 --version`
- [ ] `uv` is installed: `uv --version`
- [ ] Logseq Desktop is running with the API enabled:
  `Settings -> Features -> Enable developer mode -> Enable API server`
- [ ] `LOGSEQ_API_TOKEN` is available from Logseq:
  `Settings -> Features -> API server -> API token`

## Install

```bash
set -euo pipefail
cd /path/to/lsmcp
uv sync
```

## Run Locally

```bash
LOGSEQ_API_TOKEN=<token> uv run lsmcp
```

First run should start without a traceback and answer `health` with `{"status":"ok",...}`.

## Transports

The default transport is **streamable HTTP** (endpoint `/mcp` on `127.0.0.1:8765`). stdio is available for Claude Desktop and other stdio-only MCP clients.

```bash
# streamable HTTP (default) — no flags needed
LOGSEQ_API_TOKEN=<token> uv run lsmcp

# stdio (for Claude Desktop and stdio-only clients)
LOGSEQ_API_TOKEN=<token> uv run lsmcp --transport stdio
```

Options (flags override env vars):

| Flag | Env | Default | Notes |
|------|-----|---------|-------|
| `--transport` | `MCP_TRANSPORT` | `http` | `http` \| `stdio` \| `sse` (`http` aliases `streamable-http`) |
| `--host` | `MCP_HOST` | `127.0.0.1` | Bind host for http/sse transports |
| `--port` | `MCP_PORT` | `8765` | Bind port for http/sse transports |

`uv run lsmcp --help` prints usage. HTTP binds to loopback by default; if you expose it on `0.0.0.0`, put it behind an authenticated reverse proxy (this release does not enforce its own bearer token).

## MCP Client Config

Claude Desktop example (stdio transport, copy/paste-ready):

```json
{
  "mcpServers": {
    "lsmcp": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/path/to/lsmcp",
        "lsmcp",
        "--transport",
        "stdio"
      ],
      "cwd": "/path/to/lsmcp",
      "env": {
        "LOGSEQ_API_URL": "http://127.0.0.1:12315",
        "LOGSEQ_API_TOKEN": "<token>"
      }
    }
  }
}
```

Replace `/path/to/lsmcp` with the absolute path to your cloned repo. Cursor, VS Code MCP, and other clients use the same `command`/`args`/`cwd`/`env` shape.

## Smoke Check

```bash
uv run pytest tests/test_server.py -q

export LOGSEQ_API_TOKEN=<token>
uv run pytest tests/integration/test_mcp_stdio.py -x -q -m integration
```

Both must exit `0`. On failure: re-check the token and that Logseq's API is enabled, then run `uv run lsmcp --help`.
