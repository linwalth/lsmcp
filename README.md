# ya-logseq-mcp

Python MCP server for Logseq.

## What This Is

`ya-logseq-mcp` exposes Logseq read/write tools over MCP stdio so MCP clients can automate page and block workflows.

## Why ya-logseq-mcp

`ya-logseq-mcp` is a Python MCP server that exposes Logseq read and write operations as MCP tools, targeting Claude Desktop and any compatible MCP stdio client. It deduplicates blocks by UUID at read time and returns lean responses by default.

## Tools

| Tool | Description |
|------|-------------|
| `health` | Ping Logseq and return graph name and page count |
| `get_page` | Return a page entity and deduplicated block tree by page name |
| `get_block` | Get a single block by UUID |
| `list_pages` | List pages with optional namespace filter (truncated by limit) |
| `search_pages` | Find pages by a name fragment; returns full paths for nested/namespace pages |
| `list_namespace` | List every page beneath a namespace without truncation |
| `list_namespace_tree` | Browse a namespace as a hierarchical tree (parents with nested children) |
| `get_references` | Get backlinks to a page (pages that reference this page) |
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
cd /path/to/ya-logseq-mcp
python3 --version
uv --version
uv sync
```

## Run Locally

Script-first launch:

```bash
LOGSEQ_API_TOKEN=<token> uv run ya-logseq-mcp
```

Module fallback for troubleshooting only:

```bash
LOGSEQ_API_TOKEN=<token> uv run python -m logseq_mcp
```

expected first-run result:

- Process starts without Python traceback.
- MCP server stays attached to stdio while the client is connected.
- `health` requests return `{"status":"ok",...}` from your MCP client.

## MCP Client Config

Primary example for Claude Desktop (copy/paste-ready):

```json
{
  "mcpServers": {
    "ya-logseq-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/path/to/ya-logseq-mcp",
        "ya-logseq-mcp"
      ],
      "cwd": "/path/to/ya-logseq-mcp",
      "env": {
        "LOGSEQ_API_URL": "http://127.0.0.1:12315",
        "LOGSEQ_API_TOKEN": "<token>"
      }
    }
  }
}
```

Replace `/path/to/ya-logseq-mcp` with the absolute path to your cloned repo.

equivalent MCP clients (Cursor, VS Code MCP, custom launchers) should use the same
`command`, `args`, `cwd`, and `env` shape.
startup semantics: `command` + `args` must launch the `ya-logseq-mcp` entrypoint.
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
2. Re-run locally with `uv run ya-logseq-mcp --help`.
3. Follow troubleshooting and maintainer checks in `RUNBOOK.md`.

## Docs-Only Onboarding Verification (fresh shell)

Use this when validating docs from a clean environment:

```bash
env -i HOME="$HOME" PATH="$PATH" bash -lc '
  set -euo pipefail
  cd /path/to/ya-logseq-mcp
  python3 --version
  uv --version
  uv sync
  test -n "${LOGSEQ_API_TOKEN:-}" || echo "LOGSEQ_API_TOKEN not set in fresh shell"
  uv run ya-logseq-mcp --help >/tmp/ya-logseq-mcp-help.txt
'
```

DOCS-01 success markers:

- prereqs visible in output (`python --version`, `uv --version`)
- install command succeeds (`uv sync`)
- startup command reaches ready/no-traceback state (`ya-logseq-mcp --help` exits cleanly)

For maintainers, onboarding accuracy checks live in `RUNBOOK.md`.
