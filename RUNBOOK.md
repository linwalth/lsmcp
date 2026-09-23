# lsmcp Runbook

Internal operational notes for maintainers.

User-facing install/run/config guidance lives in `README.md` and is canonical.

## Operational Constraints

- Run commands from the repo root; `uv run` auto-detects the project via `pyproject.toml`.
- Keep MCP server key and script naming aligned to `lsmcp`.
- Treat `README.md` as the only onboarding source; this runbook should not duplicate setup steps.

## Troubleshooting

### Server does not start from MCP client

- Symptom: client reports the server is disconnected.
- Check: `cwd` in the MCP client config points at this repository root.
- Check: `LOGSEQ_API_TOKEN` is set and Logseq HTTP API is enabled.

### Quick maintainer smoke

```bash
export LOGSEQ_API_TOKEN=<token>  # or source your credentials file
uv run pytest tests/test_server.py -q
uv run pytest tests/integration/test_mcp_stdio.py -x -q -m integration
```

## README Onboarding Guardrails

Run these checks after editing onboarding docs to prevent stale or incomplete guidance:

```bash
rg -n "Requirements|Install|Run Locally|MCP Client Config|Smoke Check|equivalent MCP" README.md
rg -n "If smoke fails|Troubleshooting|RUNBOOK" README.md
uv run python -c 'import json,pathlib,re; t=pathlib.Path("README.md").read_text(); m=re.search(r"```json\\s*(\\{[\\s\\S]*?\\})\\s*```", t); assert m, "README MCP JSON snippet missing"; cfg=json.loads(m.group(1)); servers=cfg.get("mcpServers") or {}; assert servers, "mcpServers missing"; s=next(iter(servers.values())); missing=[k for k in ["command","args","cwd","env"] if k not in s]; assert not missing, missing; assert isinstance(s["args"], list) and s["args"], "args must be non-empty list"; joined=" ".join(str(x) for x in s["args"]); assert ("lsmcp" in joined) or ("-m" in s["args"] and "logseq_mcp.server" in joined), "startup semantics mismatch"; print("readme-config-parse-and-startup-ok")'
```

## Branding Consistency Check

Use this check before merging doc or metadata edits:

```bash
rg -n "lsmcp|^\[project\.urls\]" pyproject.toml src/logseq_mcp/server.py src/logseq_mcp/__init__.py README.md RUNBOOK.md
```
