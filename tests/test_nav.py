"""Tests for navigation/graph tools in logseq_mcp.tools.nav."""

import json
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")


def _make_ctx(fake_call):
    from logseq_mcp.server import AppContext

    mock_client = AsyncMock()
    mock_client._call = fake_call
    mock_ctx = AsyncMock()
    mock_ctx.request_context.lifespan_context = AppContext(client=mock_client)
    return mock_ctx


_TWO_PAGES = [
    {"id": 1, "uuid": "p1", "name": "projects/alpha", "original-name": "Projects/Alpha",
     "journal?": False, "properties": {"status": "active", "type": "project"}},
    {"id": 2, "uuid": "p2", "name": "inbox", "original-name": "Inbox",
     "journal?": False, "properties": {"status": "todo"}},
    {"id": 3, "uuid": "p3", "name": "sep 22nd, 2026", "original-name": "Sep 22nd, 2026",
     "journal?": True, "properties": {}},
]


# ---------------------------------------------------------------------------
# edn literal serialisation + interpolation
# ---------------------------------------------------------------------------

def test_edn_literal_string_escapes_quotes():
    from logseq_mcp.tools.nav import _edn_literal
    assert _edn_literal('a "b" c') == '"a \\"b\\" c"'
    assert _edn_literal("back\\slash") == '"back\\\\slash"'


def test_edn_literal_scalars_and_containers():
    from logseq_mcp.tools.nav import _edn_literal
    assert _edn_literal(None) == "nil"
    assert _edn_literal(True) == "true"
    assert _edn_literal(False) == "false"
    assert _edn_literal(42) == "42"
    assert _edn_literal(["a", 1]) == '["a" 1]'


def test_interpolate_substitutes_in_order():
    from logseq_mcp.tools.nav import _interpolate
    out = _interpolate("[:find ?x :in %1 %2 :where ...]", ["schauplätze/", 5])
    assert '"schauplätze/"' in out
    assert "5" in out
    assert "%1" not in out and "%2" not in out


def test_interpolate_out_of_range_raises():
    from logseq_mcp.tools.nav import _interpolate
    from mcp import McpError
    with pytest.raises(McpError):
        _interpolate("%1 and %2", ["only-one"])


def test_interpolate_unused_input_raises():
    from logseq_mcp.tools.nav import _interpolate
    from mcp import McpError
    with pytest.raises(McpError):
        _interpolate("no placeholders here", ["unused"])


# ---------------------------------------------------------------------------
# graph_stats
# ---------------------------------------------------------------------------

async def test_graph_stats_aggregates_namespaces(token_env):
    from logseq_mcp.tools.nav import graph_stats

    async def fake_call(method, *args):
        if method == "logseq.App.getCurrentGraph":
            return {"name": "mygraph"}
        if method == "logseq.Editor.getAllPages":
            return _TWO_PAGES
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await graph_stats(ctx))
    assert out["graph"] == "mygraph"
    assert out["total_pages"] == 3
    assert out["journals"] == 1
    assert out["non_journals"] == 2
    ns = dict(out["top_namespaces"])
    assert ns["projects"] == 1
    assert ns["inbox"] == 1


# ---------------------------------------------------------------------------
# page_outline
# ---------------------------------------------------------------------------

async def test_page_outline_flattens_and_truncates(token_env):
    from logseq_mcp.tools.nav import page_outline

    blocks = [
        {"id": 1, "uuid": "b1", "content": "top level", "children": [
            {"id": 2, "uuid": "b2", "content": "child " + "x" * 200, "children": []},
        ]},
    ]

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getPageBlocksTree"
        assert args[0] == "My Page"
        return blocks

    ctx = _make_ctx(fake_call)
    out = json.loads(await page_outline(ctx, "My Page", max_chars=20))
    assert out["page"] == "My Page"
    assert out["block_count"] == 2
    o0, o1 = out["outline"]
    assert o0["uuid"] == "b1" and o0["depth"] == 0 and o0["has_children"] is True
    assert o1["uuid"] == "b2" and o1["depth"] == 1
    assert len(o1["content"]) <= 20
    assert o1["content"].endswith("…")


# ---------------------------------------------------------------------------
# expand_references
# ---------------------------------------------------------------------------

async def test_expand_references_two_hops_bounded(token_env):
    from logseq_mcp.tools.nav import expand_references

    refs = {
        "start": [[{"name": "a", "original-name": "A"}, []]],
        "A": [[{"name": "b", "original-name": "B"}, []],
              [{"name": "start", "original-name": "start"}, []]],
        "B": [],
    }

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getPageLinkedReferences"
        return refs.get(args[0], [])

    ctx = _make_ctx(fake_call)
    out = json.loads(await expand_references(ctx, "start", hops=2, max_pages=50))
    dist = {r["page"]: r["distance"] for r in out["reached"]}
    assert dist["start"] == 0
    assert dist["A"] == 1
    assert dist["B"] == 2
    assert out["truncated"] is False


async def test_expand_references_respects_max_pages(token_env):
    from logseq_mcp.tools.nav import expand_references

    async def fake_call(method, *args):
        return [
            [{"name": f"{args[0]}-1", "original-name": f"{args[0]}-1"}, []],
            [{"name": f"{args[0]}-2", "original-name": f"{args[0]}-2"}, []],
        ]

    ctx = _make_ctx(fake_call)
    out = json.loads(await expand_references(ctx, "seed", hops=5, max_pages=10))
    assert out["truncated"] is True
    assert len(out["reached"]) <= 10


async def test_expand_references_rejects_bad_hops(token_env):
    from logseq_mcp.tools.nav import expand_references
    from mcp import McpError

    ctx = _make_ctx(AsyncMock())
    with pytest.raises(McpError):
        await expand_references(ctx, "x", hops=0)


# ---------------------------------------------------------------------------
# forward_links
# ---------------------------------------------------------------------------

async def test_forward_links_resolves_ref_ids_to_names(token_env):
    from logseq_mcp.tools.nav import forward_links

    blocks = [
        {"id": 1, "uuid": "b1", "content": "see [[Alpha]] and [[Beta]]",
         "refs": [{"id": 100}, {"id": 200}]},
    ]
    pages = [
        {"id": 100, "uuid": "pa", "name": "alpha", "original-name": "Alpha"},
        {"id": 200, "uuid": "pb", "name": "beta", "original-name": "Beta"},
        {"id": 999, "uuid": "pz", "name": "unrelated", "original-name": "Unrelated"},
    ]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return pages
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await forward_links(ctx, "some page"))
    assert out["page"] == "some page"
    assert sorted(out["links"]) == ["Alpha", "Beta"]
    assert out["count"] == 2


async def test_forward_links_no_refs_returns_empty(token_env):
    from logseq_mcp.tools.nav import forward_links

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getPageBlocksTree"
        return [{"id": 1, "uuid": "b1", "content": "lonely", "refs": []}]

    ctx = _make_ctx(fake_call)
    out = json.loads(await forward_links(ctx, "x"))
    assert out["links"] == [] and out["count"] == 0


# ---------------------------------------------------------------------------
# search_blocks
# ---------------------------------------------------------------------------

async def test_search_blocks_enriches_page_name(token_env):
    from logseq_mcp.tools.nav import search_blocks

    search_resp = {"blocks": [
        {"block/uuid": "bu1", "block/content": "hit one", "block/page": 100},
        {"block/uuid": "bu2", "block/content": "hit two", "block/page": 200},
    ]}
    pages = [
        {"id": 100, "uuid": "pa", "name": "alpha", "original-name": "Alpha"},
        {"id": 200, "uuid": "pb", "name": "beta", "original-name": "Beta"},
    ]

    async def fake_call(method, *args):
        if method == "logseq.Editor.search":
            assert args[0] == "hit"
            assert args[1] == {"limit": 20}
            return search_resp
        if method == "logseq.Editor.getAllPages":
            return pages
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await search_blocks(ctx, "hit"))
    assert out["count"] == 2
    assert out["blocks"][0]["page"] == "Alpha"
    assert out["blocks"][1]["page"] == "Beta"


async def test_search_blocks_empty_query_rejected(token_env):
    from logseq_mcp.tools.nav import search_blocks
    from mcp import McpError

    ctx = _make_ctx(AsyncMock())
    with pytest.raises(McpError):
        await search_blocks(ctx, "   ")


# ---------------------------------------------------------------------------
# namespace_stats
# ---------------------------------------------------------------------------

async def test_namespace_stats_depth_distribution(token_env):
    from logseq_mcp.tools.nav import namespace_stats

    pages = [
        {"id": 1, "name": "schauplätze/a", "original-name": "Schauplätze/A", "journal?": False},
        {"id": 2, "name": "schauplätze/sub/b", "original-name": "X", "journal?": False},
        {"id": 3, "name": "schauplätze/sub/deeper/c", "original-name": "Y", "journal?": False},
        {"id": 4, "name": "schauplätze/sub/deeper/even/d", "original-name": "Z", "journal?": False},
        {"id": 5, "name": "other/x", "original-name": "O", "journal?": False},
    ]

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getAllPages"
        return pages

    ctx = _make_ctx(fake_call)
    out = json.loads(await namespace_stats(ctx, "schauplätze"))
    assert out["namespace"] == "schauplätze"
    assert out["total_descendants"] == 4
    dd = {d["depth"]: d["pages"] for d in out["depth_distribution"]}
    assert dd[1] == 1  # schauplätze/a
    assert dd[2] == 1  # schauplätze/sub/b
    assert dd[3] == 1  # schauplätze/sub/deeper/c
    assert dd[4] == 1  # schauplätze/sub/deeper/even/d
    assert out["max_depth"] == 4


async def test_namespace_stats_empty_rejected(token_env):
    from logseq_mcp.tools.nav import namespace_stats
    from mcp import McpError

    ctx = _make_ctx(AsyncMock())
    with pytest.raises(McpError):
        await namespace_stats(ctx, "/")


# ---------------------------------------------------------------------------
# cross_reference
# ---------------------------------------------------------------------------

async def test_cross_reference_intersection(token_env):
    from logseq_mcp.tools.nav import cross_reference

    refs = {
        "A": [[{"name": "p1", "original-name": "P1"}, []],
              [{"name": "p2", "original-name": "P2"}, []]],
        "B": [[{"name": "p2", "original-name": "P2"}, []],
              [{"name": "p3", "original-name": "P3"}, []]],
    }

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getPageLinkedReferences"
        return refs.get(args[0], [])

    ctx = _make_ctx(fake_call)
    out = json.loads(await cross_reference(ctx, "A", "B"))
    assert out["shared_backlink_pages"] == ["P2"]
    assert out["count"] == 1
    assert out["a_only_count"] == 1
    assert out["b_only_count"] == 1


async def test_cross_reference_empty_name_rejected(token_env):
    from logseq_mcp.tools.nav import cross_reference
    from mcp import McpError

    ctx = _make_ctx(AsyncMock())
    with pytest.raises(McpError):
        await cross_reference(ctx, "", "B")


# ---------------------------------------------------------------------------
# query (Datalog passthrough with interpolation)
# ---------------------------------------------------------------------------

async def test_query_interpolates_placeholders(token_env):
    from logseq_mcp.tools.nav import query

    captured = {}

    async def fake_call(method, *args):
        captured["method"] = method
        captured["args"] = args
        return [{"name": "hit"}]

    ctx = _make_ctx(fake_call)
    out = json.loads(await query(ctx, "[:find ?x :where [%1 %2]]", ["schauplätze/", 5]))
    assert captured["method"] == "logseq.DB.customQuery"
    # customQuery receives ONE arg: the interpolated query string
    assert len(captured["args"]) == 1
    sent = captured["args"][0]
    assert '"schauplätze/"' in sent
    assert " 5]" in sent or " 5 " in sent
    assert "%1" not in sent
    assert out == [{"name": "hit"}]


async def test_query_unparameterised_passes_through(token_env):
    from logseq_mcp.tools.nav import query

    captured = {}

    async def fake_call(method, *args):
        captured["args"] = args
        return [825]

    ctx = _make_ctx(fake_call)
    await query(ctx, "[:find (count ?p) :where [?p :block/name]]")
    assert len(captured["args"]) == 1
    assert captured["args"][0].startswith("[:find")


async def test_query_empty_rejected(token_env):
    from logseq_mcp.tools.nav import query
    from mcp import McpError

    ctx = _make_ctx(AsyncMock())
    with pytest.raises(McpError):
        await query(ctx, "   ")


async def test_query_bad_placeholder_raises_before_call(token_env):
    from logseq_mcp.tools.nav import query
    from mcp import McpError

    called = []

    async def fake_call(method, *args):
        called.append(True)
        return []

    ctx = _make_ctx(fake_call)
    with pytest.raises(McpError):
        await query(ctx, "needs %1 and %2", ["only-one"])
    assert called == []  # must fail BEFORE hitting the API


# ---------------------------------------------------------------------------
# list_blueprints / get_blueprint
# ---------------------------------------------------------------------------

_BP_PAGES = [
    {"id": 1, "uuid": "b1", "name": "blaupausen/items", "original-name": "Blaupausen/Items", "journal?": False},
    {"id": 2, "uuid": "b2", "name": "blaupausen/npc", "original-name": "Blaupausen/NPC", "journal?": False},
    {"id": 3, "uuid": "p1", "name": "items/sword", "original-name": "Items/Sword", "journal?": False},
    {"id": 4, "uuid": "p2", "name": "kreaturen/drache", "original-name": "Kreaturen/Drache", "journal?": False},
]


async def test_list_blueprints_filters_namespace(token_env):
    from logseq_mcp.tools.nav import list_blueprints

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getAllPages"
        return _BP_PAGES

    ctx = _make_ctx(fake_call)
    out = json.loads(await list_blueprints(ctx))
    cats = [b["category"] for b in out["blueprints"]]
    assert cats == ["items", "npc"]
    assert out["count"] == 2
    assert out["namespace"] == "blaupausen"


async def test_get_blueprint_returns_outline(token_env):
    from logseq_mcp.tools.nav import get_blueprint

    blocks = [
        {"id": 1, "uuid": "h1", "content": "### Beschreibung", "children": [
            {"id": 2, "uuid": "h2", "content": "[PLACEHOLDER]", "children": []},
        ]},
    ]

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getPageBlocksTree"
        assert args[0] == "blaupausen/items"
        return blocks

    ctx = _make_ctx(fake_call)
    out = json.loads(await get_blueprint(ctx, "items"))
    assert out["blueprint"] == "blaupausen/items"
    assert out["category"] == "items"
    assert out["block_count"] == 2
    assert out["outline"][0]["content"] == "### Beschreibung"


async def test_get_blueprint_empty_category_rejected(token_env):
    from logseq_mcp.tools.nav import get_blueprint
    from mcp import McpError

    ctx = _make_ctx(AsyncMock())
    with pytest.raises(McpError):
        await get_blueprint(ctx, "  ")


# ---------------------------------------------------------------------------
# get_conventions / get_namespace_map (static tools)
# ---------------------------------------------------------------------------

async def test_get_conventions_returns_structured_json(token_env):
    from logseq_mcp.tools.nav import get_conventions
    ctx = _make_ctx(AsyncMock())
    out = json.loads(await get_conventions(ctx))
    assert "typography" in out
    assert "structure" in out
    assert "links" in out
    assert "statblock_schema" in out
    assert "no_em_dash_rule" not in out  # key is em_en_dashes
    assert "em_en_dashes" in out["typography"]
    assert "ascii" in out["typography"]["quotes"].lower()
    assert "bullet" in out["structure"]["no_bullet_prefix"].lower()


async def test_get_conventions_has_no_encoding_section(token_env):
    from logseq_mcp.tools.nav import get_conventions
    ctx = _make_ctx(AsyncMock())
    out = json.loads(await get_conventions(ctx))
    assert "encoding" not in out
    assert "filename" not in out


async def test_get_namespace_map_returns_structure(token_env):
    from logseq_mcp.tools.nav import get_namespace_map

    fake_pages = [
        {"id": 1, "uuid": "p1", "name": "schauplätze/splitter/world1", "journal?": False},
        {"id": 2, "uuid": "p2", "name": "schauplätze/splitter/world2", "journal?": False},
        {"id": 3, "uuid": "p3", "name": "kreaturen/npcs (recurring)/bob", "journal?": False},
        {"id": 4, "uuid": "p4", "name": "items/sword", "journal?": False},
        {"id": 5, "uuid": "p5", "name": "items/drogen/weed", "journal?": False},
        {"id": 6, "uuid": "p6", "name": "sep 22nd, 2026", "journal?": True},
    ]

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getAllPages"
        return fake_pages

    ctx = _make_ctx(fake_call)
    out = json.loads(await get_namespace_map(ctx))

    assert "schauplätze" in out
    assert "kreaturen" in out
    assert "items" in out
    assert "quests" in out  # in static map, not live -> page_count 0

    # live counts
    assert out["schauplätze"]["page_count"] == 2
    assert out["kreaturen"]["page_count"] == 1
    assert out["items"]["page_count"] == 2

    # static-only namespace has page_count 0
    assert out["quests"]["page_count"] == 0
    assert out["quests"]["expected"] is True

    # sub-namespaces discovered live
    spl_subs = {s["name"]: s["page_count"] for s in out["schauplätze"]["sub_namespaces"]}
    assert spl_subs["splitter"] == 2

    item_subs = {s["name"]: s["page_count"] for s in out["items"]["sub_namespaces"]}
    assert item_subs["sword"] == 1
    assert item_subs["drogen"] == 1

    # purpose carried from static map
    assert out["items"]["purpose"] is not None
    assert out["quests"]["purpose"] is not None


async def test_get_namespace_map_flags_adhoc_namespaces(token_env):
    from logseq_mcp.tools.nav import get_namespace_map

    fake_pages = [
        {"id": 1, "uuid": "p1", "name": "randomstuff/page", "journal?": False},
    ]

    async def fake_call(method, *args):
        return fake_pages

    ctx = _make_ctx(fake_call)
    out = json.loads(await get_namespace_map(ctx))
    assert "randomstuff" in out
    assert out["randomstuff"]["expected"] is False
    assert out["randomstuff"]["purpose"] is None


async def test_get_namespace_map_preserves_umlaute(token_env):
    from logseq_mcp.tools.nav import get_namespace_map

    async def fake_call(method, *args):
        return [{"id": 1, "uuid": "p1", "name": "schauplätze/x", "journal?": False}]

    ctx = _make_ctx(fake_call)
    raw = await get_namespace_map(ctx)
    assert "ä" in raw or "\\u00e4" in raw


# ---------------------------------------------------------------------------
# validate_page
# ---------------------------------------------------------------------------

async def test_validate_page_detects_dead_link(token_env):
    from logseq_mcp.tools.nav import validate_page

    blocks = [{"id": 1, "uuid": "b1", "content": "see [[Ghost Page]]", "children": []}]
    pages = [{"id": 1, "uuid": "p1", "name": "real page", "journal?": False}]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return pages
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await validate_page(ctx, "real page"))
    rules = [v["rule"] for v in out["violations"]]
    assert "dead_link" in rules
    assert out["passed"] is False


async def test_validate_page_detects_em_dash(token_env):
    from logseq_mcp.tools.nav import validate_page

    blocks = [{"id": 1, "uuid": "b1", "content": "something \u2014 or other", "children": []}]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await validate_page(ctx, "test page"))
    rules = [v["rule"] for v in out["violations"]]
    assert "em_en_dash" in rules


async def test_validate_page_exempt_dashes_inside_links(token_env):
    from logseq_mcp.tools.nav import validate_page

    blocks = [{"id": 1, "uuid": "b1", "content": "link [[Some\u2013Page]] here", "children": []}]
    pages = [{"id": 1, "uuid": "p1", "name": "some\u2013page", "journal?": False}]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return pages
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await validate_page(ctx, "test page"))
    rules = [v["rule"] for v in out["violations"]]
    assert "em_en_dash" not in rules


async def test_validate_page_detects_cjk(token_env):
    from logseq_mcp.tools.nav import validate_page

    blocks = [{"id": 1, "uuid": "b1", "content": "text with \u9f8d dragon", "children": []}]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await validate_page(ctx, "test"))
    rules = [v["rule"] for v in out["violations"]]
    assert "cjk_character" in rules


async def test_validate_page_detects_bullet_prefix(token_env):
    from logseq_mcp.tools.nav import validate_page

    blocks = [{"id": 1, "uuid": "b1", "content": "- item text", "children": []}]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await validate_page(ctx, "test"))
    rules = [v["rule"] for v in out["violations"]]
    assert "bullet_prefix" in rules


async def test_validate_page_detects_title_heading(token_env):
    from logseq_mcp.tools.nav import validate_page

    blocks = [{"id": 1, "uuid": "b1", "content": "My Page", "children": []}]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await validate_page(ctx, "My Page"))
    rules = [v["rule"] for v in out["violations"]]
    assert "title_heading" in rules


async def test_validate_page_detects_indented_heading(token_env):
    from logseq_mcp.tools.nav import validate_page

    blocks = [
        {"id": 1, "uuid": "b1", "content": "intro", "children": [
            {"id": 2, "uuid": "b2", "content": "### Nested Heading", "children": []},
        ]},
    ]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await validate_page(ctx, "test"))
    rules = [v["rule"] for v in out["violations"]]
    assert "indented_heading" in rules


async def test_validate_page_clean_passes(token_env):
    from logseq_mcp.tools.nav import validate_page

    blocks = [{"id": 1, "uuid": "b1", "content": "Valid content with [[real page]] link.", "children": []}]
    pages = [{"id": 1, "uuid": "p1", "name": "real page", "journal?": False}]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPageBlocksTree":
            return blocks
        if method == "logseq.Editor.getAllPages":
            return pages
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await validate_page(ctx, "test page"))
    assert out["passed"] is True
    assert out["count"] == 0


# ---------------------------------------------------------------------------
# orphan_report
# ---------------------------------------------------------------------------

async def test_orphan_report_classifies_correctly(token_env):
    from logseq_mcp.tools.nav import orphan_report

    pages = [
        {"id": 1, "uuid": "p1", "name": "kreaturen/monster/drache", "journal?": False},
        {"id": 2, "uuid": "p2", "name": "kreaturen/npcs (regular)/bob", "journal?": False},
        {"id": 3, "uuid": "p3", "name": "quests/main quest", "journal?": False},
        {"id": 4, "uuid": "p4", "name": "misc/random", "journal?": False},
    ]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getAllPages":
            return pages
        if method == "logseq.Editor.getPageLinkedReferences":
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await orphan_report(ctx))
    cls = {o["name"]: o["classification"] for o in out["orphans"]}
    assert cls["kreaturen/monster/drache"] == "allowed"
    assert cls["kreaturen/npcs (regular)/bob"] == "forbidden"
    assert cls["quests/main quest"] == "forbidden"
    assert cls["misc/random"] == "review"


async def test_orphan_report_skips_pages_with_backlinks(token_env):
    from logseq_mcp.tools.nav import orphan_report

    pages = [
        {"id": 1, "uuid": "p1", "name": "linked page", "journal?": False},
        {"id": 2, "uuid": "p2", "name": "orphan page", "journal?": False},
    ]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getAllPages":
            return pages
        if method == "logseq.Editor.getPageLinkedReferences":
            if args[0] == "linked page":
                return [["ref", []]]
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await orphan_report(ctx))
    names = [o["name"] for o in out["orphans"]]
    assert "orphan page" in names
    assert "linked page" not in names


async def test_orphan_report_by_classification_reflects_total(token_env):
    from logseq_mcp.tools.nav import orphan_report

    pages = [
        {"id": 1, "uuid": "p1", "name": "kreaturen/monster/m1", "journal?": False},
        {"id": 2, "uuid": "p2", "name": "kreaturen/monster/m2", "journal?": False},
        {"id": 3, "uuid": "p3", "name": "kreaturen/monster/m3", "journal?": False},
        {"id": 4, "uuid": "p4", "name": "quests/q1", "journal?": False},
    ]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getAllPages":
            return pages
        if method == "logseq.Editor.getPageLinkedReferences":
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await orphan_report(ctx, limit=2))
    assert out["shown"] == 2
    assert out["total_orphans"] == 4
    # by_classification must reflect ALL orphans, not just the shown 2
    assert out["by_classification"]["allowed"] == 3
    assert out["by_classification"]["forbidden"] == 1


async def test_orphan_report_limits_results(token_env):
    from logseq_mcp.tools.nav import orphan_report

    pages = [{"id": i, "uuid": f"p{i}", "name": f"kreaturen/monster/m{i}", "journal?": False} for i in range(10)]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getAllPages":
            return pages
        if method == "logseq.Editor.getPageLinkedReferences":
            return []
        raise AssertionError(method)

    ctx = _make_ctx(fake_call)
    out = json.loads(await orphan_report(ctx, limit=3))
    assert out["shown"] == 3
    assert out["total_orphans"] == 10


# ---------------------------------------------------------------------------
# similar_pages
# ---------------------------------------------------------------------------

async def test_similar_pages_finds_near_duplicates(token_env):
    from logseq_mcp.tools.nav import similar_pages

    pages = [
        {"id": 1, "uuid": "p1", "name": "items/sword", "journal?": False},
        {"id": 2, "uuid": "p2", "name": "items/swurd", "journal?": False},
        {"id": 3, "uuid": "p3", "name": "kreaturen/drache", "journal?": False},
    ]

    async def fake_call(method, *args):
        assert method == "logseq.Editor.getAllPages"
        return pages

    ctx = _make_ctx(fake_call)
    out = json.loads(await similar_pages(ctx, threshold=0.7))
    pair_names = [(p["a"], p["b"]) for p in out["pairs"]]
    assert any(("items/sword", "items/swurd") == pn or ("items/swurd", "items/sword") == pn for pn in pair_names)


async def test_similar_pages_excludes_identical(token_env):
    from logseq_mcp.tools.nav import similar_pages

    pages = [
        {"id": 1, "uuid": "p1", "name": "dup", "journal?": False},
        {"id": 2, "uuid": "p2", "name": "dup", "journal?": False},
    ]

    async def fake_call(method, *args):
        return pages

    ctx = _make_ctx(fake_call)
    out = json.loads(await similar_pages(ctx, threshold=0.5))
    assert out["count"] == 0


async def test_similar_pages_threshold_validation(token_env):
    from logseq_mcp.tools.nav import similar_pages
    from mcp import McpError

    ctx = _make_ctx(AsyncMock())
    with pytest.raises(McpError):
        await similar_pages(ctx, threshold=1.5)
