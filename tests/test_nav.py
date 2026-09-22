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
