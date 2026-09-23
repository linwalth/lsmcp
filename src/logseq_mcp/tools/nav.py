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
from difflib import SequenceMatcher
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


_BLUEPRINT_NAMESPACE = "blaupausen"


@mcp.tool()
async def list_blueprints(ctx: Context) -> str:
    """List all page-blueprint templates in the graph.

    Blueprints live under the 'blaupausen/' namespace and define the expected
    block STRUCTURE for pages of each category (items, npc, spells, monsters,
    quests, ...). Each blueprint contains headed sections with placeholder
    prompts. CALL THIS before `page_create` to discover which categories have a
    template, then use `get_blueprint` to fetch the structure to replicate.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("list_blueprints")

    raw = await client._call("logseq.Editor.getAllPages")
    pages_raw = raw if isinstance(raw, list) else []

    blueprints: list[dict] = []
    for page_raw in pages_raw:
        try:
            page = PageEntity.model_validate(page_raw)
        except Exception:
            continue
        if page.name and page.name.startswith(_BLUEPRINT_NAMESPACE + "/"):
            category = page.name[len(_BLUEPRINT_NAMESPACE) + 1:]
            blueprints.append({"category": category, "page": page.display_name or page.name})

    blueprints.sort(key=lambda b: b["category"])
    return json.dumps({"namespace": _BLUEPRINT_NAMESPACE, "blueprints": blueprints, "count": len(blueprints)})


@mcp.tool()
async def get_blueprint(
    ctx: Context,
    category: Annotated[str, Field(description="Blueprint category (e.g. 'items', 'npc', 'spells'). Matches a page under blaupausen/.")],
) -> str:
    """Fetch the full block structure of a page blueprint so it can be replicated.

    Returns the headed sections and placeholder prompts from the blueprint page
    `blaupausen/<category>`. Use this AFTER `list_blueprints` to see the exact
    structure a new page of that category should follow, then pass that structure
    to `page_create` as initial blocks (stripping placeholder brackets and filling
    in real content). This ensures newly created pages match your graph's
    conventions instead of ad-hoc layouts.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    cat = (category or "").strip().strip("/")
    if not cat:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="category must be a non-empty string"))

    page_name = f"{_BLUEPRINT_NAMESPACE}/{cat}"
    logger.info("get_blueprint: %s", page_name)

    raw = await client._call("logseq.Editor.getPageBlocksTree", page_name)
    blocks_raw = raw if isinstance(raw, list) else []

    parsed: list[BlockEntity] = []
    for b in blocks_raw:
        try:
            parsed.append(BlockEntity.model_validate(b))
        except Exception:
            continue

    outline = _flatten_outline(parsed, max_chars=200)
    return json.dumps({"blueprint": page_name, "category": cat, "block_count": len(outline), "outline": outline})


_CONVENTIONS = {
    "typography": {
        "em_en_dashes": "Forbidden in prose. Replace by context: sentence boundary '. ', apposition ', ', stats whitespace only, number ranges '-'. Dashes inside [[...]] links, asset paths, and properties are exempt.",
        "quotes": "ASCII quotes only: \" and '. No smart/curly quotes.",
        "umlauts": "Write ä ö ü ß as real umlauts, never ae oe ue ss. Exception: proper nouns consistently spelled without umlauts.",
        "no_cjk": "No CJK unified ideographs in wiki content. Transliterate or translate Asian-origin terms.",
        "sentences_end_with_period": "Narrative/prose bullets always end with a period. Fragments (HP, AC, labels, stats) do not. Does not apply to statblocks and technical lists.",
    },
    "structure": {
        "bullet_style": "Bullets, not flowing prose. Related facts in one bullet, sub-bullets for details, max depth 3. No doubled '- -' (Logseq already makes a bullet from '-'). Detailed descriptions nonetheless. Links to everything relevant.",
        "no_bullet_prefix": "Do not prefix block content with '- ' or '*' — every Logseq block is inherently a bullet, so leading dashes render literally.",
        "headings_root_level": "All headings on root level, never indented. '### Heading', not '- ### Heading' or '\\t### Heading'. Heading depth (# to ######) is preserved. No exceptions.",
        "section_labels": "Section labels as '### Label' headings (without '**', Logseq renders bold). Statblock headings all '### '.",
        "no_title_heading": "First line of a page must not repeat the file name. Logseq displays the file name as the title.",
        "no_block_ids": "No block IDs (^BLOCKNAME).",
    },
    "links": {
        "notation": "Slash notation: [[Namespace/Page]]. Physical files use ___ separator internally (invisible to writer).",
        "existing_only": "Links may only point to EXISTING pages. Verify the target exists before linking. Dead-links create empty pages on click and must be cleaned up manually. If a target does not exist, write as plain text (without [[]]) or create the page first. Exception: deliberate forward-references filled promptly (e.g. quest links on new dungeons).",
        "no_pipe_aliases": "[[Namespace/Name|Name]] is forbidden when the alias equals the last path segment — Logseq creates an empty page on click. Use [[Namespace/Name]] instead.",
        "alias_syntax": "Pipe-syntax [[Namespace/Name|Alias]] does NOT work in Logseq (that is Obsidian syntax). Correct Logseq syntax: [Display Text]([[Namespace/Page]]). Example: [die lachende Katze]([[Organisationen/Gilden und Verbünde/Die lachende Katze - Händlergilde]]).",
        "no_inline_chains": "No comma-separated inline link chains. Expand to nested bullets instead.",
        "child_locations": "Child locations link to parent splitter: '- Liegt auf [[...]]'.",
    },
    "game_design": {
        "no_quest_tags": "No tags for plothooks/quests. Plothooks under '### Plothooks' on location pages. Quests in the Quests/ namespace.",
        "ley_not_resource": "Ley is cosmic energy, omnipresent, connected to water. Not mineable, not controllable like a mine.",
        "fantastic_realism": "Adapt real-world processes and principles for the fantasy world. The user has the final word.",
        "metropolregionen": "Cities under a same-named region are named 'Metropolregion X/X'. Region: 'Metropolregion Baroly', city below: 'Baroly'. Exception: hub/dungeon pairs (e.g. Ana-Noxys/Ana-Noxys) are not metropolregionen and stay untouched.",
    },
    "statblock_schema": {
        "mandatory_fields": "Description (short prose, max 5-8 lines), CR / difficulty, HP, AC, movement, STR/DEX/CON/INT/WIS/CHA (with modifier in parentheses), type, alignment, actions.",
        "optional_fields": "Bonus actions, reactions, free actions (bosses only).",
        "no_basismod": "Basismod is deprecated. Use six attribute scores (STR/DEX/CON/INT/WIS/CHA) with modifiers. Proficiency bonus (Ubungsbonus) is player-characters only, never in NPC/creature statblocks.",
        "art_label": "**Art:** not **Rasse:** or **Volk:** (universal for humanoid, monstrosity, undead, etc.).",
        "free_actions": "'Freie Aktionen' replaces 'Villain-Aktionen'/' Bosewichtaktionen'.",
        "dungeon_indent": "Dungeon statblocks retain double indentation (\\t\\t-) under '#### [Name]' within '### Encounters'.",
    },
    "images": "Image and cover-image embeds (![bild](../assets/...)) are tolerated on ALL pages of ALL schemas. Neither blueprints nor audits object to image embeds. Images do not disturb any schema.",
}


_NAMESPACE_MAP = {
    "schauplätze": {
        "splitter": "World shards and locations (Schauplätze/Splitter/)",
    },
    "kreaturen": {
        "npcs_recurring": "Recurring NPCs and humanoid encounter groups (Kreaturen/NPCs (Recurring)/)",
        "npcs_regular": "One-shot NPCs (Kreaturen/NPCs (Regular)/)",
        "npcs_retired": "Dead/retired NPCs (Kreaturen/NPCs (Retired)/)",
        "tiere": "Animal creatures (Kreaturen/Tiere/)",
        "fahrzeuge": "Airships, planes, ships (Kreaturen/Fahrzeuge/)",
        "monster": "Non-humanoid monsters, constructs, ghosts (Kreaturen/Monster/)",
    },
    "organisationen": "Religions and cults, guilds and unions, military and factions, groups, syndicates, gangs, families and clans",
    "götter und höhere wesen": "Gods, saints, higher principles",
    "items": {
        "_root": "Items, curses, relics",
        "drogen": "Drug sub-category (Items/Drogen/)",
    },
    "spells": "Homebrew spells",
    "quests": "Quests with '### Prämisse' minimum",
    "blaupausen": "19 page-structure templates (categories: Splitter, Städte, Dörfer, Gegenden, Dungeons, Natur, Magisch, Zivilisation, Quest, NPC, Encounter, Monster, Organisation, Kompendium, Götter, Items, Spells, Tiere, Fahrzeuge)",
}


@mcp.tool()
async def get_conventions(ctx: Context) -> str:
    """Return the graph's WRITING CONVENTIONS as structured JSON.

    Covers typography (no em-dashes, ASCII quotes, real umlauts, no CJK), structure
    (bullet style, heading rules, no title-heading, no block-IDs), link syntax
    ([[Namespace/Page]], existing-only, no pipe-aliases, correct alias syntax),
    game-design rules (no quest tags, ley-is-not-a-resource, fantastic realism,
    metropolregion naming), and the statblock schema (mandatory/optional fields,
    no basismod, art label, dungeon indentation). Call this BEFORE writing any
    narrative content via block_append, block_prepend, block_update,
    journal_append, or page_create.
    """
    logger.info("get_conventions")
    return json.dumps(_CONVENTIONS, ensure_ascii=False)


@mcp.tool()
async def get_namespace_map(ctx: Context) -> str:
    """Return the graph's NAMESPACE ARCHITECTURE, merging static conventions
    with LIVE page counts from the graph.

    Combines two layers:
    - STATIC PURPOSE: the intended architecture (where each page type belongs,
      e.g. recurring NPCs under Kreaturen/NPCs (Recurring)/).
    - LIVE DISCOVERY: actual top-level namespaces and their sub-namespaces,
      with page counts, queried from getAllPages.

    Each namespace entry carries: `purpose` (from conventions, if known),
    `page_count` (live), `sub_namespaces` (live, with counts), and `expected`
    (true if the namespace appears in the static architecture, false if it is
    unexpected/ad-hoc). Call this before page_create or rename_page to ensure
    correct placement.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("get_namespace_map")

    raw = await client._call("logseq.Editor.getAllPages")
    pages_raw = raw if isinstance(raw, list) else []

    # Collect live top-level namespaces and their sub-namespaces with counts.
    top_counts: Counter[str] = Counter()
    sub_counts: dict[str, Counter[str]] = {}

    for page_raw in pages_raw:
        try:
            page = PageEntity.model_validate(page_raw)
        except Exception:
            continue
        if not page.name or page.journal:
            continue
        parts = page.name.split("/", 2)
        top = parts[0]
        top_counts[top] += 1
        if len(parts) >= 2:
            sub_key = parts[1] if len(parts) == 2 else parts[1]
            sub_counts.setdefault(top, Counter())[sub_key] += 1

    # Build merged result.
    result: dict[str, dict] = {}
    all_namespaces = set(top_counts.keys()) | set(_NAMESPACE_MAP.keys())

    for ns in sorted(all_namespaces):
        purpose = _NAMESPACE_MAP.get(ns)
        entry: dict = {
            "page_count": top_counts.get(ns, 0),
            "expected": ns in _NAMESPACE_MAP,
        }
        if isinstance(purpose, dict):
            entry["purpose"] = "; ".join(str(v) for v in purpose.values())
            entry["sub_namespace_purposes"] = purpose
        elif isinstance(purpose, str):
            entry["purpose"] = purpose
        elif purpose is None:
            entry["purpose"] = None

        subs_live = sub_counts.get(ns, Counter())
        if subs_live:
            entry["sub_namespaces"] = [
                {"name": sn, "page_count": cnt}
                for sn, cnt in sorted(subs_live.items())
            ]
        else:
            entry["sub_namespaces"] = []

        result[ns] = entry

    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Validation, orphan detection, and duplicate detection
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_EM_DASH = "\u2014"
_EN_DASH = "\u2013"
_SMART_QUOTES = ("\u201c", "\u201d", "\u2018", "\u2019")

_ORPHAN_ALLOWED_NAMESPACES = frozenset({
    "kreaturen/monster", "kreaturen/tiere", "götter und höhere wesen",
    "blaupausen", "spells",
})
_ORPHAN_FORBIDDEN_NAMESPACES = frozenset({
    "kreaturen/npcs (recurring)", "kreaturen/npcs (regular)",
    "quests", "items", "schauplätze",
})


def _classify_orphan(name: str) -> str:
    lower = name.lower()
    for ns in _ORPHAN_FORBIDDEN_NAMESPACES:
        if lower.startswith(ns):
            return "forbidden"
    for ns in _ORPHAN_ALLOWED_NAMESPACES:
        if lower.startswith(ns):
            return "allowed"
    return "review"


def _strip_links_and_images(text: str) -> str:
    text = _IMAGE_RE.sub("", text)
    text = _LINK_RE.sub("", text)
    return text


def _extract_outgoing_links(blocks: list[BlockEntity]) -> list[str]:
    links: list[str] = []
    for block in blocks:
        for m in _LINK_RE.finditer(block.content or ""):
            raw = m.group(1)
            if "|" in raw:
                raw = raw.rsplit("|", 1)[0]
            links.append(raw.strip())
        if block.children:
            links.extend(_extract_outgoing_links(block.children))
    return links


@mcp.tool()
async def validate_page(
    ctx: Context,
    name: Annotated[str, Field(description="Page name to validate (natural casing).")],
) -> str:
    """Lint a single page against the graph's writing conventions.

    Checks for: DEAD LINKS (outgoing [[...]] links whose target page does not
    exist), EM/EN-DASHES in prose (outside links/images), SMART QUOTES, CJK
    characters, BULLET-PREFIX contamination ('- ' or '* ' at block start),
    TITLE-HEADING redundancy (first block repeats the page name). Returns a
    structured violations list with severity, location, and a suggested fix.
    Call this AFTER page_create or block_append to catch mistakes before they
    propagate.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("validate_page: %s", name)

    raw = await client._call("logseq.Editor.getPageBlocksTree", name)
    blocks_raw = raw if isinstance(raw, list) else []

    parsed: list[BlockEntity] = []
    for b in blocks_raw:
        try:
            parsed.append(BlockEntity.model_validate(b))
        except Exception:
            continue

    # Build name-set for dead-link check (cached getAllPages).
    all_pages_raw = await client._call("logseq.Editor.getAllPages")
    existing_names: set[str] = set()
    if isinstance(all_pages_raw, list):
        for p in all_pages_raw:
            try:
                page = PageEntity.model_validate(p)
                if page.name:
                    existing_names.add(page.name.lower())
            except Exception:
                continue

    violations: list[dict] = []

    # Dead links
    outgoing = _extract_outgoing_links(parsed)
    for link in outgoing:
        if link.lower() not in existing_names:
            violations.append({
                "severity": "high",
                "rule": "dead_link",
                "detail": f"[[{link}]] — target page does not exist",
                "fix": "create the target page, or write as plain text without [[]]",
            })

    # Content scans
    def scan_block(block: BlockEntity, depth: int) -> None:
        content = block.content or ""

        # Title-heading check (first top-level block only)
        if depth == 0 and content.strip().lower() == name.strip().lower():
            violations.append({
                "severity": "low",
                "rule": "title_heading",
                "detail": f"first block repeats page name '{name}'",
                "fix": "remove the redundant title; Logseq displays the file name",
            })

        # Bullet-prefix check
        stripped = content.lstrip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            violations.append({
                "severity": "medium",
                "rule": "bullet_prefix",
                "detail": f"block starts with bullet marker: {content[:50]}",
                "fix": "remove leading '- ' or '* '; every Logseq block is already a bullet",
            })

        # Strip links/images before dash/quote/CJK checks (they are exempt inside links)
        prose = _strip_links_and_images(content)

        if _EM_DASH in prose or _EN_DASH in prose:
            char = _EM_DASH if _EM_DASH in prose else _EN_DASH
            violations.append({
                "severity": "medium",
                "rule": "em_en_dash",
                "detail": f"found '{char}' in: {content[:60]}",
                "fix": "replace with '. ' (sentence), ', ' (apposition), '-' (range), or space (stats)",
            })

        for sq in _SMART_QUOTES:
            if sq in prose:
                violations.append({
                    "severity": "low",
                    "rule": "smart_quote",
                    "detail": f"found smart quote '{sq}' in: {content[:60]}",
                    "fix": "use ASCII '\"' or \"'\"",
                })

        if _CJK_RE.search(prose):
            cjk_match = _CJK_RE.search(prose)
            violations.append({
                "severity": "high",
                "rule": "cjk_character",
                "detail": f"found CJK character '{cjk_match.group()}' in: {content[:60]}" if cjk_match else "CJK detected",
                "fix": "transliterate or translate; no Chinese characters in wiki content",
            })

        for child in block.children or []:
            scan_block(child, depth + 1)

    for block in parsed:
        scan_block(block, 0)

    return json.dumps({
        "page": name,
        "violations": violations,
        "count": len(violations),
        "passed": len(violations) == 0,
    }, ensure_ascii=False)


@mcp.tool()
async def orphan_report(
    ctx: Context,
    limit: Annotated[int, Field(description="Max orphan pages to return (0=all). Default 100.")] = 100,
) -> str:
    """Report pages with ZERO incoming backlinks, classified by orphan policy.

    Iterates all non-journal pages and checks each for backlinks via
    getPageLinkedReferences. Pages with no incoming links are orphans. Each is
    classified as 'allowed' (Monster, Tiere, Götter, Blaupausen, Spells —
    intentional reference material), 'forbidden' (NPCs, Quests, Items, Orte —
    should be linked from a hub), or 'review' (everything else). Call this for
    graph hygiene — orphaned story-relevant pages indicate missing connections.
    Runs many API calls; expect ~10-20 seconds on a large graph.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("orphan_report: limit=%d", limit)

    all_pages_raw = await client._call("logseq.Editor.getAllPages")
    if not isinstance(all_pages_raw, list):
        return json.dumps({"orphans": [], "count": 0})

    pages: list[PageEntity] = []
    for p in all_pages_raw:
        try:
            page = PageEntity.model_validate(p)
            if page.name and not page.journal:
                pages.append(page)
        except Exception:
            continue

    orphans: list[dict] = []

    async def check_one(page: PageEntity) -> dict | None:
        refs = await client._call("logseq.Editor.getPageLinkedReferences", page.name)
        if isinstance(refs, list) and len(refs) > 0:
            return None
        return {
            "name": page.display_name or page.name,
            "classification": _classify_orphan(page.name),
        }

    tasks = [check_one(p) for p in pages]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, dict):
            orphans.append(r)

    orphans.sort(key=lambda o: (o["classification"], o["name"].lower()))
    total = len(orphans)
    if limit > 0:
        orphans = orphans[:limit]

    by_class = Counter(o["classification"] for o in orphans)
    return json.dumps({
        "orphans": orphans,
        "shown": len(orphans),
        "total_orphans": total,
        "by_classification": dict(by_class),
    }, ensure_ascii=False)


@mcp.tool()
async def similar_pages(
    ctx: Context,
    threshold: Annotated[float, Field(description="Similarity ratio threshold 0-1 (default 0.85). Higher = stricter match.")] = 0.85,
    limit: Annotated[int, Field(description="Max pairs to return (0=all). Default 50.")] = 50,
) -> str:
    """Find near-duplicate page names via fuzzy string matching.

    Compares all non-journal page names pairwise using difflib similarity ratio.
    Catches casing-induced duplicates ('items/sword' vs 'Items/Sword'),
    spelling variants, and accidental near-collisions. Returns pairs with their
    similarity score. Call this after bulk imports or when suspecting duplicate
    pages. Threshold 0.85 is strict enough to catch typos but loose enough for
    meaningful near-misses.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    if threshold < 0 or threshold > 1:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="threshold must be between 0 and 1"))

    logger.info("similar_pages: threshold=%.2f limit=%d", threshold, limit)

    all_pages_raw = await client._call("logseq.Editor.getAllPages")
    if not isinstance(all_pages_raw, list):
        return json.dumps({"pairs": [], "count": 0})

    names: list[str] = []
    for p in all_pages_raw:
        try:
            page = PageEntity.model_validate(p)
            if page.name and not page.journal:
                names.append(page.display_name or page.name)
        except Exception:
            continue

    pairs: list[dict] = []
    names_sorted = sorted(names, key=str.lower)
    n = len(names_sorted)
    for i in range(n):
        for j in range(i + 1, n):
            ratio = SequenceMatcher(None, names_sorted[i].lower(), names_sorted[j].lower()).ratio()
            if ratio >= threshold and ratio < 1.0:
                pairs.append({
                    "a": names_sorted[i],
                    "b": names_sorted[j],
                    "similarity": round(ratio, 3),
                })
        if limit > 0 and len(pairs) >= limit:
            break

    pairs.sort(key=lambda p: p["similarity"], reverse=True)
    if limit > 0:
        pairs = pairs[:limit]

    return json.dumps({"pairs": pairs, "count": len(pairs)}, ensure_ascii=False)
