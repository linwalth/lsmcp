"""Navigation and graph-aware tools: Datalog passthrough plus progressive-disclosure
helpers built on the existing read RPCs.

These tools help an agent orient itself in a large graph cheaply (stats, outlines,
backlink expansion, forward links, content search) and run server-side filtered
queries via Logseq's indexed Datascript DB instead of pulling every page into
Python.

NOTE on Datalog over HTTP: Logseq's HTTP API does NOT auto-bind the `$` database
symbol for `:in` clauses, so parameterised queries of the form
`[:in $ ?x] ...` fail with "Too few inputs passed". This module sidesteps that by
interpolating caller-supplied values DIRECTLY into the query string as quoted
edn literals via `%1`, `%2`, ... placeholders. Do not use `:in` with external
inputs — inline them as `%N` instead.
"""

import asyncio
import json
import logging
import re
from collections import Counter, deque
from typing import Annotated

from mcp import McpError
from mcp.server.fastmcp import Context
from mcp.types import ErrorData, INTERNAL_ERROR
from pydantic import Field

from logseq_mcp.server import AppContext, mcp
from logseq_mcp.types import BlockEntity, PageEntity

logger = logging.getLogger(__name__)


_PLACEHOLDER_RE = re.compile(r"%(\d+)")


def _edn_literal(value: object) -> str:
    """Serialize a Python scalar/container into a safe edn literal for inlining."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + " ".join(_edn_literal(v) for v in value) + "]"
    # Fallback: stringify as a quoted string.
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _interpolate(datalog: str, inputs: list) -> str:
    """Replace `%1`, `%2`, ... placeholders in `datalog` with edn-literal inputs.

    Raises McpError if a referenced placeholder has no corresponding input or
    vice versa.
    """
    indices_seen: set[int] = set()

    def repl(match: re.Match) -> str:
        idx = int(match.group(1))
        if idx < 1 or idx > len(inputs):
            raise McpError(
                ErrorData(
                    code=INTERNAL_ERROR,
                    message=f"placeholder %{idx} out of range (have {len(inputs)} input(s))",
                )
            )
        indices_seen.add(idx)
        return _edn_literal(inputs[idx - 1])

    interpolated = _PLACEHOLDER_RE.sub(repl, datalog)
    unused = [i + 1 for i in range(len(inputs)) if (i + 1) not in indices_seen]
    if unused:
        raise McpError(
            ErrorData(
                code=INTERNAL_ERROR,
                message=f"inputs at positions {unused} were not referenced by any %N placeholder",
            )
        )
    return interpolated


@mcp.tool()
async def query(
    ctx: Context,
    datalog: Annotated[str, Field(description="Datalog query string (Logseq advanced-query edn syntax). Inline caller values via %1,%2,... placeholders (do NOT use :in $ — HTTP API won't bind it).")],
    inputs: Annotated[list | None, Field(description="Values for %1,%2,... placeholders, in order. Each is substituted as a quoted edn literal. Null/omit for unparameterised queries.")] = None,
) -> str:
    """Run a Datalog query against Logseq's indexed Datascript DB.

    Pushes filtering, joins, and sorting INTO Logseq (server-side) instead of
    fetching all pages/blocks and filtering in Python — far faster on large
    graphs. Use it for anything the dedicated tools cannot express: content
    regex scans, structural predicates, cross-reference intersections.

    `datalog` is a Datalog query string in Logseq's advanced-query convention,
    for example:
      "[:find (pull ?b [*]) :where [?b :block/marker \"TODO\"]]"

    Because Logseq's HTTP API does not auto-bind the `$` database symbol,
    parameterised `:in $ ?x` clauses do NOT work remotely. Inline caller values
    via `%1`, `%2`, ... placeholders instead and pass them in `inputs` (1-based,
    in order). The tool substitutes each placeholder with a safely-quoted edn
    literal before sending. Example:

      datalog: '[:find (pull ?p [:block/name]) :where [?p :block/name ?n]
                  [(clojure.string/starts-with? ?n %1)]]'
      inputs:  ["schauplätze/"]

    Every placeholder must have a matching input and vice versa, or the call
    raises. Unparameterised queries (no `%N`) take `inputs=null`. Behaviour
    follows Logseq's `logseq.DB.customQuery` RPC; raw API errors are surfaced.
    This is a power-user passthrough — prefer the typed tools when they suffice.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    stripped = (datalog or "").strip()
    if not stripped:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="datalog query must be a non-empty string"))

    args_list = list(inputs) if inputs else []
    final_query = _interpolate(stripped, args_list) if args_list else stripped

    logger.info("query: datalog(%d chars) inputs=%d", len(final_query), len(args_list))

    try:
        result = await client._call("logseq.DB.customQuery", final_query)
    except McpError:
        raise
    except Exception as exc:
        raise McpError(
            ErrorData(
                code=INTERNAL_ERROR,
                message=f"Datalog query failed: {exc}",
            )
        ) from exc

    return json.dumps(result, default=str)


async def _id_to_name_map(client) -> dict[int, str]:
    """Cached helper: Logseq page id -> display name, from getAllPages."""
    raw = await client._call("logseq.Editor.getAllPages")
    pages_raw = raw if isinstance(raw, list) else []
    mapping: dict[int, str] = {}
    for page_raw in pages_raw:
        try:
            page = PageEntity.model_validate(page_raw)
        except Exception:
            continue
        if page.id and page.name:
            mapping[page.id] = page.display_name or page.name
    return mapping


@mcp.tool()
async def graph_stats(ctx: Context) -> str:
    """Cheap orientation snapshot of the whole graph.

    Returns the graph name plus aggregate counts computed from the cached page
    listing: total pages, journal vs non-journal counts, and the top namespaces by
    page count. Call this FIRST to understand the shape of the graph before
    drilling into specific pages. Costs one getAllPages round trip (shared/
    cacheable with list_pages/search_pages).
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("graph_stats")

    graph = await client._call("logseq.App.getCurrentGraph")
    raw = await client._call("logseq.Editor.getAllPages")
    pages_raw = raw if isinstance(raw, list) else []

    pages: list[PageEntity] = []
    for page_raw in pages_raw:
        try:
            page = PageEntity.model_validate(page_raw)
        except Exception:
            continue
        if page.name:
            pages.append(page)

    total = len(pages)
    journal_count = sum(1 for p in pages if p.journal)
    non_journal = total - journal_count

    namespace_counts: Counter[str] = Counter()
    for p in pages:
        if p.journal:
            continue
        top = p.name.split("/", 1)[0]
        namespace_counts[top] += 1

    graph_name = graph.get("name", "unknown") if isinstance(graph, dict) else "unknown"

    return json.dumps(
        {
            "graph": graph_name,
            "total_pages": total,
            "journals": journal_count,
            "non_journals": non_journal,
            "top_namespaces": namespace_counts.most_common(20),
        }
    )


def _truncate(text: str, limit: int = 140) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _flatten_outline(blocks: list[BlockEntity], depth: int = 0, max_chars: int = 140) -> list[dict]:
    out: list[dict] = []
    for block in blocks:
        out.append(
            {
                "uuid": block.uuid,
                "depth": depth,
                "content": _truncate(block.content, max_chars),
                "marker": block.marker,
                "has_children": bool(block.children),
            }
        )
        if block.children:
            out.extend(_flatten_outline(block.children, depth + 1, max_chars))
    return out


@mcp.tool()
async def page_outline(
    ctx: Context,
    name: Annotated[str, Field(description="Page name (natural casing).")],
    max_chars: Annotated[int, Field(description="Truncate each block's content preview to this many chars (default 140).")] = 140,
) -> str:
    """Return a FLAT outline skeleton of a page — block uuids with indented depth
    and TRUNCATED content, no nested children payloads.

    Use this for progressive disclosure: scan the structure of a page cheaply
    before deciding which blocks to fetch in full with `get_block`. Much lighter
    on tokens than `get_page` when you only need to see what a page contains.
    `max_chars` caps each block's preview (default 140).
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("page_outline: %s", name)

    raw = await client._call("logseq.Editor.getPageBlocksTree", name)
    blocks_raw = raw if isinstance(raw, list) else []

    parsed: list[BlockEntity] = []
    seen: set[str] = set()
    for b in blocks_raw:
        try:
            block = BlockEntity.model_validate(b)
        except Exception:
            continue
        if block.uuid in seen:
            continue
        seen.add(block.uuid)
        parsed.append(block)

    outline = _flatten_outline(parsed, max_chars=max_chars)
    return json.dumps({"page": name, "block_count": len(outline), "outline": outline})


@mcp.tool()
async def expand_references(
    ctx: Context,
    name: Annotated[str, Field(description="Starting page name (natural casing). Expansion begins here at distance 0.")],
    hops: Annotated[int, Field(description="BFS radius (default 1). 1 = direct backlinks, 2 = backlinks of backlinks, etc.")] = 1,
    max_pages: Annotated[int, Field(description="Hard cap on total reached pages to avoid runaway expansion on hubs (default 50).")] = 50,
) -> str:
    """Expand the BACKLINK neighbourhood around a page by up to `hops` BFS levels.

    Starts at `name`, collects pages that REFERENCE it (level 1), then pages that
    reference THOSE (level 2), and so on — navigating the wikilink graph inward
    (who points at me, who points at them, ...). For the OUTWARD direction (what
    does this page link to?) use `forward_links`.

    `hops` (default 1) bounds the radius; `max_pages` (default 50) caps total
    reached pages to avoid runaway expansion on densely linked hubs. Each reached
    page carries its distance from the start. Returns reached page names only —
    fetch individual pages with `get_page` when you need their content.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    if hops < 1:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="hops must be >= 1"))
    if max_pages < 1:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="max_pages must be >= 1"))

    logger.info("expand_references: %s hops=%d max=%d", name, hops, max_pages)

    reached: dict[str, int] = {name: 0}
    frontier: deque[str] = deque([name])

    for level in range(1, hops + 1):
        if not frontier:
            break
        next_frontier: deque[str] = deque()
        while frontier:
            current = frontier.popleft()
            raw = await client._call("logseq.Editor.getPageLinkedReferences", current)
            if not isinstance(raw, list):
                continue
            for ref in raw:
                if not isinstance(ref, list) or len(ref) < 2:
                    continue
                page_raw = ref[0]
                try:
                    page = PageEntity.model_validate(page_raw)
                except Exception:
                    continue
                neighbor = page.display_name or page.name
                if not neighbor or neighbor in reached:
                    continue
                reached[neighbor] = level
                next_frontier.append(neighbor)
                if len(reached) >= max_pages:
                    return json.dumps(
                        {
                            "start": name,
                            "hops": hops,
                            "direction": "backlinks",
                            "reached": [
                                {"page": p, "distance": d} for p, d in reached.items()
                            ],
                            "truncated": True,
                        }
                    )
        frontier = next_frontier

    return json.dumps(
        {
            "start": name,
            "hops": hops,
            "direction": "backlinks",
            "reached": [{"page": p, "distance": d} for p, d in reached.items()],
            "truncated": False,
        }
    )


def _collect_ref_ids(blocks: list[BlockEntity], acc: set[int]) -> None:
    """Walk a block tree collecting integer page-ids from `refs` (and children)."""
    for block in blocks:
        for ref in getattr(block, "refs", None) or []:
            if isinstance(ref, dict) and isinstance(ref.get("id"), int):
                acc.add(ref["id"])
            elif isinstance(ref, int):
                acc.add(ref)
        if block.children:
            _collect_ref_ids(block.children, acc)


@mcp.tool()
async def forward_links(
    ctx: Context,
    name: Annotated[str, Field(description="Page name whose outbound wikilinks to list (natural casing).")],
) -> str:
    """List the pages that THIS page links to (outbound wikilinks).

    The complement of `get_references` (which gives inbound backlinks). Walks the
    page's block tree, harvests every `[[wikilink]]` reference, and resolves the
    integer page-ids to human-readable page names via the cached page listing.
    Useful for "what does this lore entry connect to?" exploration without
    loading each linked page's content.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("forward_links: %s", name)

    raw = await client._call("logseq.Editor.getPageBlocksTree", name)
    blocks_raw = raw if isinstance(raw, list) else []

    parsed: list[BlockEntity] = []
    for b in blocks_raw:
        try:
            parsed.append(BlockEntity.model_validate(b))
        except Exception:
            continue

    ref_ids: set[int] = set()
    _collect_ref_ids(parsed, ref_ids)

    if not ref_ids:
        return json.dumps({"page": name, "links": [], "count": 0})

    id_map = await _id_to_name_map(client)
    links: list[str] = []
    for rid in sorted(ref_ids):
        nm = id_map.get(rid)
        if nm:
            links.append(nm)

    return json.dumps({"page": name, "links": links, "count": len(links)})


@mcp.tool()
async def search_blocks(
    ctx: Context,
    query_text: Annotated[str, Field(description="Full-text search term matched against block CONTENTS (not page names). e.g. 'drache', 'AC 16'.")],
    limit: Annotated[int, Field(description="Max matching blocks to return (default 20).")] = 20,
) -> str:
    """Full-text SEARCH over BLOCK CONTENTS via Logseq's indexed search.

    Unlike `search_pages` (which matches page NAMES only), this scans the actual
    text inside blocks — the primary way to find lore by substance ("drache",
    "AC 16", a spell name) without loading every page. Returns matching blocks
    with their uuid, truncated content, and the PAGE NAME that contains them
    (resolved from the cached page listing; the raw API returns only an opaque
    page id). `limit` caps results (default 20).
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    stripped = (query_text or "").strip()
    if not stripped:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="query must be a non-empty string"))
    if limit < 1:
        limit = 20

    logger.info("search_blocks: %r limit=%d", stripped, limit)

    raw = await client._call("logseq.Editor.search", stripped, {"limit": limit})
    if not isinstance(raw, dict):
        return json.dumps({"query": stripped, "blocks": [], "count": 0})

    blocks_raw = raw.get("blocks", []) or []
    id_map = await _id_to_name_map(client)

    out = []
    for b in blocks_raw:
        if not isinstance(b, dict):
            continue
        uid = b.get("block/uuid") or b.get("uuid") or ""
        content = b.get("block/content") or b.get("content") or ""
        page_id = b.get("block/page")
        if isinstance(page_id, dict):
            page_id = page_id.get("id")
        page_name = id_map.get(page_id, "") if isinstance(page_id, int) else ""
        out.append(
            {
                "uuid": uid,
                "page": page_name,
                "content": _truncate(content, 200),
            }
        )

    return json.dumps({"query": stripped, "blocks": out, "count": len(out)})


@mcp.tool()
async def namespace_stats(
    ctx: Context,
    namespace: Annotated[str, Field(description="Namespace to analyse (e.g. 'schauplätze' or 'kreaturen'). Case-insensitive.")],
) -> str:
    """Granular statistics for a single namespace subtree.

    Whereas `graph_stats` surveys the WHOLE graph, this drills into one
    namespace: total descendant pages, max depth, depth distribution, and the
    breakdown of immediate child sub-namespaces with their page counts. Ideal for
    deciding how to traverse a large hierarchy (e.g. "schauplätze" with hundreds
    of pages across 5 levels) before paging into it.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    ns = (namespace or "").strip().strip("/")
    if not ns:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="namespace must be a non-empty string"))

    logger.info("namespace_stats: %s", ns)

    raw = await client._call("logseq.Editor.getAllPages")
    pages_raw = raw if isinstance(raw, list) else []

    ns_lower = ns.lower()
    descendants: list[PageEntity] = []
    for page_raw in pages_raw:
        try:
            page = PageEntity.model_validate(page_raw)
        except Exception:
            continue
        if not page.name:
            continue
        if page.journal:
            continue
        if page.name.lower() == ns_lower or page.name.lower().startswith(ns_lower + "/"):
            descendants.append(page)

    total = len(descendants)
    depth_dist: Counter[int] = Counter()
    max_depth = 0
    child_ns: Counter[str] = Counter()
    prefix_len = len(ns_lower) + 1  # skip "ns/"
    for p in descendants:
        remainder = p.name[p.name.lower().index(ns_lower) + prefix_len:] \
            if p.name.lower().startswith(ns_lower + "/") else ""
        depth = 0 if not remainder else remainder.count("/") + 1
        depth_dist[depth] += 1
        if depth > max_depth:
            max_depth = depth
        if remainder:
            top_child = remainder.split("/", 1)[0]
            child_ns[f"{ns}/{top_child}"] += 1

    return json.dumps(
        {
            "namespace": ns,
            "total_descendants": total,
            "max_depth": max_depth,
            "depth_distribution": [
                {"depth": d, "pages": c} for d, c in sorted(depth_dist.items())
            ],
            "child_namespaces": child_ns.most_common(),
        }
    )


@mcp.tool()
async def cross_reference(
    ctx: Context,
    a: Annotated[str, Field(description="First page name (natural casing).")],
    b: Annotated[str, Field(description="Second page name (natural casing).")],
) -> str:
    """Find pages that link to BOTH `a` and `b` (intersection of their backlinks).

    A common graph query that's tedious to express by hand: "which lore pages
    reference both this creature AND this location?" Computes the set
    intersection of backlink sources for the two pages. Returns the shared page
    names only — load them with `get_page` for context. Both pages must exist.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    na = (a or "").strip()
    nb = (b or "").strip()
    if not na or not nb:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="both page names must be non-empty"))

    logger.info("cross_reference: %s ∩ %s", na, nb)

    async def backlink_set(name: str) -> set[str]:
        raw = await client._call("logseq.Editor.getPageLinkedReferences", name)
        if not isinstance(raw, list):
            return set()
        out: set[str] = set()
        for ref in raw:
            if not isinstance(ref, list) or len(ref) < 2:
                continue
            try:
                page = PageEntity.model_validate(ref[0])
            except Exception:
                continue
            nm = page.display_name or page.name
            if nm:
                out.add(nm)
        return out

    set_a, set_b = await asyncio.gather(backlink_set(na), backlink_set(nb))
    shared = sorted(set_a & set_b)

    return json.dumps(
        {
            "a": na,
            "b": nb,
            "shared_backlink_pages": shared,
            "count": len(shared),
            "a_only_count": len(set_a - set_b),
            "b_only_count": len(set_b - set_a),
        }
    )
