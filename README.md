# lsmcp

Python MCP server for Logseq.

## What This Is

`lsmcp` exposes Logseq read/write tools over MCP stdio so MCP clients can automate page and block workflows.

## Why lsmcp

`lsmcp` is a Python MCP server that exposes Logseq read and write operations as MCP tools, targeting Claude Desktop and any compatible MCP stdio client. It deduplicates blocks by UUID at read time and returns lean responses by default.

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

Complete this checklist before install:

- [ ] Python 3.12+ is installed: `python3 --version`
- [ ] `uv` is installed: `uv --version`
- [ ] Logseq Desktop is running with API enabled:
  `Settings -> Features -> Enable developer mode -> Enable API server`
- [ ] `LOGSEQ_API_TOKEN` is available from Logseq:
  `Settings -> Features -> API server -> API token`

## Install

Clone or copy the repo, then run from the repo root:

```bash
set -euo pipefail
cd /path/to/lsmcp
python3 --version
uv --version
uv sync
```

## Run Locally

Script-first launch:

```bash
LOGSEQ_API_TOKEN=<token> uv run lsmcp
```

Module fallback for troubleshooting only:

```bash
LOGSEQ_API_TOKEN=<token> uv run python -m logseq_mcp
```

expected first-run result:

- Process starts without Python traceback.
- MCP server stays attached to stdio while the client is connected.
- `health` requests return `{"status":"ok",...}` from your MCP client.

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

`uv run lsmcp --help` prints usage. HTTP binds to loopback by default; if you expose it on `0.0.0.0`, put it behind an authenticated reverse proxy (the server does not enforce its own bearer token in this release).

## MCP Client Config

Primary example for Claude Desktop (stdio transport, copy/paste-ready):

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

Replace `/path/to/lsmcp` with the absolute path to your cloned repo.

equivalent MCP clients (Cursor, VS Code MCP, custom launchers) should use the same
`command`, `args`, `cwd`, and `env` shape.
startup semantics: `command` + `args` must launch the `lsmcp` entrypoint.
parse validation for this JSON block is included in the verification commands for this plan.

## Smoke Check

Run checks in this order:

1. Unit smoke:

```bash
uv run pytest tests/test_server.py -q
```

2. MCP stdio integration smoke:

```bash
export LOGSEQ_API_TOKEN=<token>
uv run pytest tests/integration/test_mcp_stdio.py -x -q -m integration
```

Pass/fail interpretation:

- PASS: pytest exits `0` and reports passing tests.
- FAIL: pytest exits non-zero or reports failures/errors.

If smoke fails:

1. Re-check token and API availability (`LOGSEQ_API_TOKEN`, Logseq API enabled).
2. Re-run locally with `uv run lsmcp --help`.
3. Follow troubleshooting and maintainer checks in `RUNBOOK.md`.

## Docs-Only Onboarding Verification (fresh shell)

Use this when validating docs from a clean environment:

```bash
env -i HOME="$HOME" PATH="$PATH" bash -lc '
  set -euo pipefail
  cd /path/to/lsmcp
  python3 --version
  uv --version
  uv sync
  test -n "${LOGSEQ_API_TOKEN:-}" || echo "LOGSEQ_API_TOKEN not set in fresh shell"
  uv run lsmcp --help >/tmp/lsmcp-help.txt
'
```

DOCS-01 success markers:

- prereqs visible in output (`python --version`, `uv --version`)
- install command succeeds (`uv sync`)
- startup command reaches ready/no-traceback state (`lsmcp --help` exits cleanly)

For maintainers, onboarding accuracy checks live in `RUNBOOK.md`.
