import json
import logging

from typing import Annotated

from mcp import McpError
from mcp.types import ErrorData, INTERNAL_ERROR
from mcp.server.fastmcp import Context
from pydantic import Field

from logseq_mcp.server import mcp, AppContext
from logseq_mcp.types import BlockEntity, PageEntity

logger = logging.getLogger(__name__)


@mcp.tool()
async def health(ctx: Context) -> str:
    """Ping Logseq and return graph name and page count."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("health: checking Logseq connectivity")

    graph = await client._call("logseq.App.getCurrentGraph")
    pages = await client._call("logseq.Editor.getAllPages")

    graph_name = graph.get("name", "unknown") if isinstance(graph, dict) else "unknown"
    page_count = len(pages) if isinstance(pages, list) else 0

    return json.dumps({"status": "ok", "graph": graph_name, "page_count": page_count})


def _count_blocks(blocks: list[BlockEntity]) -> int:
    total = 0
    for block in blocks:
        total += 1 + _count_blocks(block.children)
    return total


def _dedup_children(children: list[BlockEntity], seen: set[str]) -> list[BlockEntity]:
    filtered: list[BlockEntity] = []
    for child in children:
        if child.uuid in seen:
            continue
        seen.add(child.uuid)
        child.children = _dedup_children(child.children, seen)
        filtered.append(child)
    return filtered


def _parse_block_tree(raw_blocks: list) -> list[BlockEntity]:
    seen: set[str] = set()
    parsed: list[BlockEntity] = []

    for raw_block in raw_blocks:
        block = BlockEntity.model_validate(raw_block)
        if block.uuid in seen:
            continue
        seen.add(block.uuid)
        block.children = _dedup_children(block.children, seen)
        parsed.append(block)

    return parsed


@mcp.tool()
async def get_page(
    ctx: Context,
    name: Annotated[str, Field(description="Page name with natural casing (e.g. 'Meeting Notes 2026'). Resolved case-insensitively.")],
) -> str:
    """Return a page entity and deduplicated block tree by page name.

    Logseq stores page identities case-insensitively: the canonical `name` is kept
    lowercase as a unique id, while `original-name` preserves the casing used when
    the page was created or last referenced. Pass `name` with natural casing and
    orthography (for example "Meeting Notes 2026"); Logseq resolves it
    case-insensitively. The returned page surfaces the human-readable name rather
    than the lowercased storage slug, so callers should reuse it verbatim instead
    of reintroducing a lowercased reference (which Logseq would adopt as the new
    display name).
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("get_page: fetching page %s", name)

    page_raw = await client._call("logseq.Editor.getPage", name)
    if page_raw is None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"page not found: {name}"))

    page = PageEntity.model_validate(page_raw)
    page_view = page.model_dump(by_alias=False)
    page_view["name"] = page.display_name

    blocks_result = await client._call("logseq.Editor.getPageBlocksTree", name)
    blocks_raw = blocks_result if isinstance(blocks_result, list) else []
    blocks = _parse_block_tree(blocks_raw)

    return json.dumps(
        {
            "page": page_view,
            "blocks": [block.model_dump(by_alias=False) for block in blocks],
            "block_count": _count_blocks(blocks),
        }
    )


@mcp.tool()
async def get_block(
    ctx: Context,
    uuid: Annotated[str, Field(description="Block UUID (not a page name). From get_page/page_outline/etc.")],
    include_children: Annotated[bool, Field(description="If true (default), return the block's child subtree too.")] = True,
) -> str:
    """Get a single block by UUID, optionally with its child subtree.

    Returns the block's content, properties, marker, and (by default) recursively
    expanded children. Identify the block by UUID — use `get_page` or
    `page_outline` first to discover UUIDs. Set `include_children=false` for a
    lightweight fetch of just the block itself.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("get_block: %s", uuid)

    opts = {"includeChildren": include_children}
    raw = await client._call("logseq.Editor.getBlock", uuid, opts)
    if raw is None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"block not found: {uuid}"))

    block = BlockEntity.model_validate(raw)
    return json.dumps(block.model_dump(by_alias=False))


@mcp.tool()
async def list_pages(
    ctx: Context,
    namespace: Annotated[str, Field(description="Optional namespace PREFIX to filter by (e.g. 'projects' matches 'projects/alpha'). Case-insensitive. Empty = all pages.")] = "",
    include_journals: Annotated[bool, Field(description="If true, include journal/day pages. Default false (exclude journals).")] = False,
    limit: Annotated[int, Field(description="Max pages to return (alphabetical by storage name). 0 = unlimited. Default 50. NOTE: truncates late-alpha/deep-nested pages.")] = 50,
    slim: Annotated[bool, Field(description="If true, drop per-page 'properties' — return only name+journal (lighter).")] = False,
) -> str:
    """List pages, optionally narrowed to a namespace prefix.

    NOTE: this returns AT MOST `limit` pages (default 50) sorted alphabetically by
    the lowercased storage name, so deeply nested or late-alphabetical pages
    (including most namespace pages like "Worldbuilding/...") can be cut off. To
    find a page when you only know a fragment of its name, use `search_pages`
    instead; to list EVERYTHING beneath a namespace without truncation, use
    `list_namespace`.

    `namespace` is a case-insensitive prefix matched against the full page path
    (for example "projects" matches "projects/alpha" and "projects/beta"). Pass
    natural casing; results surface the human-readable page name. Pass
    `slim=True` to drop per-page `properties` and return only name + journal —
    cheaper to scan when you are just browsing for a page name.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("list_pages: namespace=%r limit=%d slim=%s", namespace, limit, slim)

    raw = await client._call("logseq.Editor.getAllPages")
    pages_raw = raw if isinstance(raw, list) else []

    pages: list[PageEntity] = []
    for page_raw in pages_raw:
        page = PageEntity.model_validate(page_raw)
        if not page.name:
            continue
        if not include_journals and page.journal:
            continue
        if namespace and not page.name.lower().startswith(namespace.lower()):
            continue
        pages.append(page)

    pages.sort(key=lambda page: page.name.lower())
    if limit > 0:
        pages = pages[:limit]

    result = [_page_summary(page, slim=slim) for page in pages]
    return json.dumps(result)


def _page_summary(page: PageEntity, slim: bool = False) -> dict:
    summary: dict = {
        "name": page.display_name,
        "journal": page.journal,
    }
    if not slim:
        summary["properties"] = page.properties
    return summary


def _leaf_segment(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _search_rank(page: PageEntity, query_cf: str) -> tuple[int, int, int, str]:
    """Rank matches so the page the user most likely means sorts first.

    Order: exact leaf match (0) < leaf contains query (1) < match only in a
    namespace segment (2); then shallower pages before deeper ones; then
    alphabetical by lowercased storage name as a stable tiebreaker.
    """
    name_cf = page.name.casefold()
    leaf = _leaf_segment(name_cf)
    if leaf == query_cf:
        bucket = 0
    elif query_cf in leaf:
        bucket = 1
    else:
        bucket = 2
    depth = page.name.count("/") + 1
    return (bucket, depth, len(page.name), name_cf)


@mcp.tool()
async def search_pages(
    ctx: Context,
    query: Annotated[str, Field(description="Fragment of a page name to search for (case-insensitive). Matches both display name and storage slug.")],
    include_journals: Annotated[bool, Field(description="If true, include journal pages in results. Default false.")] = False,
    limit: Annotated[int, Field(description="Max results. 0 = all matches. Default 50.")] = 50,
    slim: Annotated[bool, Field(description="If true, drop per-page 'properties' — lighter results.")] = False,
    within_namespace: Annotated[str, Field(description="Restrict matches to pages beneath this namespace (e.g. 'schauplätze'). Empty = search everywhere.")] = "",
) -> str:
    """Find pages by a fragment of their name — the primary way to locate a page
    when you do NOT know its exact full path (for example a nested namespace page
    like "Worldbuilding/Regions/Eastern Sea").

    Matching is case-insensitive and checks BOTH the human-readable name and the
    lowercased storage name, so a query like "eastern" finds the page above and
    returns its FULL path. Results are ranked: an exact match on the page's final
    path segment ranks first, then a segment that contains the query, then matches
    in a parent namespace. Pass `query` with natural casing; `limit=0` returns all
    matches. Pass `slim=True` to drop per-page `properties`. Pass
    `within_namespace="schauplätze"` to restrict matches to pages beneath that
    namespace (handy in large graphs organised by namespaces). Use this BEFORE
    guessing a full page name for `get_page`/`block_append`.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    stripped = query.strip()
    if not stripped:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="query must be a non-empty string"))
    query_cf = stripped.casefold()
    ns_cf = within_namespace.strip().strip("/").lower()

    logger.info("search_pages: query=%r limit=%d slim=%s ns=%r", stripped, limit, slim, ns_cf)

    raw = await client._call("logseq.Editor.getAllPages")
    pages_raw = raw if isinstance(raw, list) else []

    matches: list[PageEntity] = []
    for page_raw in pages_raw:
        page = PageEntity.model_validate(page_raw)
        if not page.name:
            continue
        if not include_journals and page.journal:
            continue
        if ns_cf and not page.name.lower().startswith(ns_cf + "/") and page.name.lower() != ns_cf:
            continue
        haystack = (page.name + "\n" + page.original_name).casefold()
        if query_cf not in haystack:
            continue
        matches.append(page)

    matches.sort(key=lambda page: _search_rank(page, query_cf))
    if limit > 0:
        matches = matches[:limit]

    return json.dumps([_page_summary(page, slim=slim) for page in matches])


@mcp.tool()
async def list_namespace(
    ctx: Context,
    namespace: Annotated[str, Field(description="Namespace path without slashes (e.g. 'projects' or 'worldbuilding/regions'). Case-insensitive.")],
    include_journals: Annotated[bool, Field(description="If true, include journal pages. Default false.")] = False,
    limit: Annotated[int, Field(description="Cap on results. 0 (default) = all matching pages, no truncation.")] = 0,
) -> str:
    """List every page beneath a namespace, without the alphabetical truncation
    that affects `list_pages`. Use this to browse a hierarchy when you know a
    parent namespace but not the leaf page (for example pass "worldbuilding" to
    see all of "worldbuilding/...", or "worldbuilding/regions" to drill one level
    deeper).

    `namespace` is a top-level or nested namespace path WITHOUT leading or trailing
    slashes (for example "projects" or "worldbuilding/regions"). Resolution is
    case-insensitive. `limit=0` (the default) returns all matching pages; pass a
    positive number to cap the result. If you only know a name fragment and not a
    namespace, use `search_pages` instead.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    ns = namespace.strip().strip("/")
    if not ns:
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message="namespace must be a non-empty string")
        )

    logger.info("list_namespace: namespace=%r limit=%d", ns, limit)

    raw = await client._call("logseq.Editor.getPagesFromNamespace", ns)
    pages_raw = raw if isinstance(raw, list) else []

    pages: list[PageEntity] = []
    for page_raw in pages_raw:
        page = PageEntity.model_validate(page_raw)
        if not page.name:
            continue
        if not include_journals and page.journal:
            continue
        pages.append(page)

    pages.sort(key=lambda page: page.name.lower())
    if limit > 0:
        pages = pages[:limit]

    return json.dumps([_page_summary(page) for page in pages])


def _namespace_tree_node(raw: dict, include_journals: bool) -> dict | None:
    """Build a hierarchical node from a raw getPagesTreeFromNamespace entry."""
    page = PageEntity.model_validate(raw)
    if not page.name:
        return None
    if not include_journals and page.journal:
        return None

    children_raw = raw.get("children", [])
    children = []
    if isinstance(children_raw, list):
        for child_raw in children_raw:
            if isinstance(child_raw, dict):
                child = _namespace_tree_node(child_raw, include_journals)
                if child is not None:
                    children.append(child)

    node = _page_summary(page)
    node["children"] = children
    return node


@mcp.tool()
async def list_namespace_tree(
    ctx: Context,
    namespace: Annotated[str, Field(description="Top-level or nested namespace path without slashes (e.g. 'worldbuilding'). Case-insensitive.")],
    include_journals: Annotated[bool, Field(description="If true, include journal pages in the tree. Default false.")] = False,
) -> str:
    """Browse a namespace as a HIERARCHICAL TREE (parents with nested children).

    Unlike `list_namespace` (flat list), this preserves the parent→child structure
    so you can drill into sub-namespaces visually. Use this when you want to
    understand the layout of a namespace before picking a specific page — for
    example passing "worldbuilding" returns a tree showing "worldbuilding/regions/
    ..." with regions' pages nested beneath it.

    `namespace` is a top-level or nested namespace path WITHOUT leading or
    trailing slashes. Resolution is case-insensitive. Journal pages are excluded
    by default; pass `include_journals=True` to include them. If you only need a
    flat list of page names, use `list_namespace` instead.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    ns = namespace.strip().strip("/")
    if not ns:
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message="namespace must be a non-empty string")
        )

    logger.info("list_namespace_tree: namespace=%r", ns)

    raw = await client._call("logseq.Editor.getPagesTreeFromNamespace", ns)
    pages_raw = raw if isinstance(raw, list) else []

    tree = []
    for page_raw in pages_raw:
        if isinstance(page_raw, dict):
            node = _namespace_tree_node(page_raw, include_journals)
            if node is not None:
                tree.append(node)

    return json.dumps(tree)


@mcp.tool()
async def get_references(
    ctx: Context,
    name: Annotated[str, Field(description="Page name whose backlinks to retrieve (natural casing).")],
) -> str:
    """Get backlinks to a page — pages that CONTAIN a wikilink to this page.

    Returns the referring pages and the specific blocks within them that link
    here. For the reverse direction (what does THIS page link to?) use
    `forward_links`. For multi-hop traversal use `expand_references`.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("get_references: %s", name)

    raw = await client._call("logseq.Editor.getPageLinkedReferences", name)
    if not isinstance(raw, list):
        return json.dumps([])

    backlinks = []
    for ref in raw:
        if not isinstance(ref, list) or len(ref) < 2:
            continue

        page_raw, blocks_raw = ref[0], ref[1]
        try:
            page = PageEntity.model_validate(page_raw)
            blocks = [BlockEntity.model_validate(block_raw) for block_raw in (blocks_raw or [])]
        except Exception:
            continue

        backlinks.append(
            {
                "page": page.display_name,
                "blocks": [{"uuid": block.uuid, "content": block.content} for block in blocks],
            }
        )

    return json.dumps(backlinks)
