"""Failing test stubs for Phase 3 write tools — RED state.

Tests cover WRIT-01 through WRIT-05:
  WRIT-01: page_create creates a page with properties and optional initial blocks
  WRIT-02: block_append accepts flat strings and nested objects
  WRIT-03: block_append preserves hierarchy and ordering
  WRIT-04: block_update updates content and verifies readback
  WRIT-05: block_delete removes a block and verifies absence

All tests import their tools inside the test body so pytest can still
collect the file before write tool implementation is complete.
"""

import json
from unittest.mock import AsyncMock

import pytest
from mcp import McpError


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")


def _make_ctx(mock_client=None):
    from logseq_mcp.server import AppContext

    client = mock_client or AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.request_context.lifespan_context = AppContext(client=client)
    return mock_ctx


def _flatten_block_values(blocks: list[dict], field: str) -> list[str]:
    values: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if field in block:
            values.append(block[field])
        children = block.get("children", [])
        if children:
            values.extend(_flatten_block_values(children, field))
    return values


async def test_page_create_with_properties_and_initial_blocks(token_env):
    from logseq_mcp.tools.write import page_create

    page_name = "Project Alpha"
    page_properties = {"type": "Project", "status": "active"}
    initial_blocks = [
        "root 1",
        {"content": "root 2", "properties": {"kind": "task"}, "children": [{"content": "child"}]},
    ]

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.createPage":
            return {"uuid": "page-uuid", "name": page_name, "properties": page_properties}
        if method == "logseq.Editor.appendBlockInPage":
            return {"uuid": "root-1"}
        if method == "logseq.Editor.insertBatchBlock":
            return []
        if method == "logseq.Editor.getPage":
            return {
                "id": 1,
                "uuid": "page-uuid",
                "name": page_name,
                "original-name": page_name,
                "properties": page_properties,
            }
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {"id": 10, "uuid": "root-1", "content": "root 1", "children": []},
                {
                    "id": 11,
                    "uuid": "root-2",
                    "content": "root 2",
                    "properties": {"kind": "task"},
                    "children": [{"id": 12, "uuid": "child-1", "content": "child", "children": []}],
                },
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await page_create(
        mock_ctx,
        name=page_name,
        properties=page_properties,
        blocks=initial_blocks,
    )
    data = json.loads(result)

    assert calls[0] == (
        "logseq.Editor.createPage",
        (page_name, page_properties, {"createFirstBlock": False}),
    )
    assert [method for method, _ in calls[1:3]] == [
        "logseq.Editor.appendBlockInPage",
        "logseq.Editor.insertBatchBlock",
    ]
    assert data["page"]["properties"] == page_properties
    assert _flatten_block_values(data["blocks"], "content") == ["root 1", "root 2", "child"]


async def test_block_append_accepts_strings_and_nested_objects(token_env):
    from logseq_mcp.tools.write import block_append

    payload = [
        "root 1",
        {"content": "root 2", "properties": {"kind": "task"}, "children": [{"content": "child"}]},
    ]
    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            return {"id": 1, "uuid": "page-uuid", "name": "Project Alpha", "original-name": "Project Alpha"}
        if method == "logseq.Editor.appendBlockInPage":
            return {"uuid": "uuid-root-1"}
        if method == "logseq.Editor.insertBatchBlock":
            return []
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {"id": 10, "uuid": "uuid-root-1", "content": "root 1", "children": []},
                {
                    "id": 11,
                    "uuid": "uuid-root-2",
                    "content": "root 2",
                    "properties": {"kind": "task"},
                    "children": [{"id": 12, "uuid": "uuid-child-1", "content": "child", "children": []}],
                },
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_append(mock_ctx, page="Project Alpha", blocks=payload)
    data = json.loads(result)

    append_calls = [call for call in calls if call[0] == "logseq.Editor.appendBlockInPage"]
    assert len(append_calls) == 1
    assert append_calls[0][1][1] == "root 1"
    batch_calls = [call for call in calls if call[0] == "logseq.Editor.insertBatchBlock"]
    assert len(batch_calls) == 1
    assert batch_calls[0][1][0] == "uuid-root-1"
    assert batch_calls[0][1][2] == {"sibling": True}
    assert data["page"] == "Project Alpha"
    assert data["appended"] == 3
    assert data["block_count"] == 3
    assert _flatten_block_values(data["blocks"], "content") == ["root 1", "root 2", "child"]


async def test_block_append_preserves_requested_hierarchy_and_order(token_env):
    from logseq_mcp.tools.write import block_append

    payload = [
        {"content": "parent a", "children": [{"content": "child a1"}, {"content": "child a2"}]},
        "parent b",
    ]
    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            return {"id": 1, "uuid": "page-uuid", "name": "Project Alpha", "original-name": "Project Alpha"}
        if method == "logseq.Editor.appendBlockInPage":
            return {"uuid": "uuid-parent-a"}
        if method == "logseq.Editor.insertBatchBlock":
            return []
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {
                    "id": 10,
                    "uuid": "uuid-parent-a",
                    "content": "parent a",
                    "children": [
                        {"id": 11, "uuid": "uuid-child-a1", "content": "child a1", "children": []},
                        {"id": 12, "uuid": "uuid-child-a2", "content": "child a2", "children": []},
                    ],
                },
                {"id": 13, "uuid": "uuid-parent-b", "content": "parent b", "children": []},
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_append(mock_ctx, page="Project Alpha", blocks=payload)
    data = json.loads(result)

    assert [method for method, _ in calls[1:5]] == [
        "logseq.Editor.appendBlockInPage",
        "logseq.Editor.insertBatchBlock",
        "logseq.Editor.insertBatchBlock",
        "logseq.Editor.getPageBlocksTree",
    ]
    assert calls[2][1][0] == "uuid-parent-a"
    assert calls[2][1][2] == {"sibling": False}
    assert calls[3][1][0] == "uuid-parent-a"
    assert calls[3][1][2] == {"sibling": True}
    assert data["appended"] == 4
    assert data["block_count"] == 4
    assert [block["content"] for block in data["blocks"]] == ["parent a", "parent b"]
    assert [child["content"] for child in data["blocks"][0]["children"]] == ["child a1", "child a2"]


async def test_journal_append_accepts_strings_and_nested_objects(token_env):
    from logseq_mcp.tools.write import journal_append

    payload = [
        "journal root 1",
        {
            "content": "journal root 2",
            "properties": {"kind": "task"},
            "children": [{"content": "journal child"}],
        },
    ]
    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            if len(calls) == 1:
                return None
            return {
                "id": 7,
                "uuid": "journal-page-uuid",
                "name": "2026-04-11",
                "original-name": "2026-04-11",
                "journal?": True,
            }
        if method == "logseq.Editor.createPage":
            return {"uuid": "journal-page-uuid", "name": "2026-04-11", "journal?": True}
        if method == "logseq.Editor.appendBlockInPage":
            return {"uuid": "journal-root-1"}
        if method == "logseq.Editor.insertBatchBlock":
            return []
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {"id": 10, "uuid": "journal-root-1", "content": "journal root 1", "children": []},
                {
                    "id": 11,
                    "uuid": "journal-root-2",
                    "content": "journal root 2",
                    "properties": {"kind": "task"},
                    "children": [
                        {"id": 12, "uuid": "journal-child-1", "content": "journal child", "children": []}
                    ],
                },
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await journal_append(mock_ctx, date="2026-04-11", blocks=payload)
    data = json.loads(result)

    assert [method for method, _ in calls] == [
        "logseq.Editor.getPage",
        "logseq.Editor.createPage",
        "logseq.Editor.getPageBlocksTree",
        "logseq.Editor.appendBlockInPage",
        "logseq.Editor.insertBatchBlock",
        "logseq.Editor.getPageBlocksTree",
    ]
    assert calls[1] == (
        "logseq.Editor.createPage",
        ("2026-04-11", {}, {"createFirstBlock": False}),
    )
    assert data["page"] == "2026-04-11"
    assert data["appended"] == 3
    assert data["block_count"] == 3
    assert _flatten_block_values(data["blocks"], "content") == [
        "journal root 1",
        "journal root 2",
        "journal child",
    ]


async def test_journal_append_preserves_requested_hierarchy_and_order(token_env):
    from logseq_mcp.tools.write import journal_append

    payload = [
        {"content": "journal parent", "children": [{"content": "journal child a"}, {"content": "journal child b"}]},
        "journal sibling",
    ]
    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            return {
                "id": 7,
                "uuid": "journal-page-uuid",
                "name": "2026-04-12",
                "original-name": "2026-04-12",
                "journal?": True,
            }
        if method == "logseq.Editor.appendBlockInPage":
            return {"uuid": "journal-parent-uuid"}
        if method == "logseq.Editor.insertBatchBlock":
            return []
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {
                    "id": 21,
                    "uuid": "journal-parent-uuid",
                    "content": "journal parent",
                    "children": [
                        {"id": 22, "uuid": "journal-child-a-uuid", "content": "journal child a", "children": []},
                        {"id": 23, "uuid": "journal-child-b-uuid", "content": "journal child b", "children": []},
                    ],
                },
                {"id": 24, "uuid": "journal-sibling-uuid", "content": "journal sibling", "children": []},
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await journal_append(mock_ctx, date="2026-04-12", blocks=payload)
    data = json.loads(result)

    assert [method for method, _ in calls] == [
        "logseq.Editor.getPage",
        "logseq.Editor.getPageBlocksTree",
        "logseq.Editor.appendBlockInPage",
        "logseq.Editor.insertBatchBlock",
        "logseq.Editor.insertBatchBlock",
        "logseq.Editor.getPageBlocksTree",
    ]
    assert data["appended"] == 4
    assert data["block_count"] == 4
    assert [block["content"] for block in data["blocks"]] == ["journal parent", "journal sibling"]
    assert [child["content"] for child in data["blocks"][0]["children"]] == [
        "journal child a",
        "journal child b",
    ]


async def test_block_prepend_uses_prepend_api_and_returns_prepended_count(token_env):
    from logseq_mcp.tools.write import block_prepend

    payload = [
        "root 1",
        {"content": "root 2", "properties": {"kind": "task"}, "children": [{"content": "child"}]},
    ]
    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            return {"id": 1, "uuid": "page-uuid", "name": "Project Alpha", "original-name": "Project Alpha"}
        if method == "logseq.Editor.prependBlockInPage":
            return {"uuid": "uuid-root-1"}
        if method == "logseq.Editor.insertBatchBlock":
            return []
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {"id": 10, "uuid": "uuid-root-1", "content": "root 1", "children": []},
                {
                    "id": 11,
                    "uuid": "uuid-root-2",
                    "content": "root 2",
                    "properties": {"kind": "task"},
                    "children": [{"id": 12, "uuid": "uuid-child-1", "content": "child", "children": []}],
                },
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_prepend(mock_ctx, page="Project Alpha", blocks=payload)
    data = json.loads(result)

    prepend_calls = [call for call in calls if call[0] == "logseq.Editor.prependBlockInPage"]
    assert len(prepend_calls) == 1
    assert prepend_calls[0][1][1] == "root 1"
    batch_calls = [call for call in calls if call[0] == "logseq.Editor.insertBatchBlock"]
    assert len(batch_calls) == 1
    assert batch_calls[0][1][0] == "uuid-root-1"
    assert batch_calls[0][1][2] == {"sibling": True}
    assert data["page"] == "Project Alpha"
    assert data["prepended"] == 3
    assert data["block_count"] == 3
    assert _flatten_block_values(data["blocks"], "content") == ["root 1", "root 2", "child"]


async def test_block_prepend_preserves_hierarchy_with_children_batch(token_env):
    from logseq_mcp.tools.write import block_prepend

    payload = [
        {"content": "parent a", "children": [{"content": "child a1"}, {"content": "child a2"}]},
        "parent b",
    ]
    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            return {"id": 1, "uuid": "page-uuid", "name": "Project Alpha", "original-name": "Project Alpha"}
        if method == "logseq.Editor.prependBlockInPage":
            return {"uuid": "uuid-parent-a"}
        if method == "logseq.Editor.insertBatchBlock":
            return []
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {
                    "id": 10,
                    "uuid": "uuid-parent-a",
                    "content": "parent a",
                    "children": [
                        {"id": 11, "uuid": "uuid-child-a1", "content": "child a1", "children": []},
                        {"id": 12, "uuid": "uuid-child-a2", "content": "child a2", "children": []},
                    ],
                },
                {"id": 13, "uuid": "uuid-parent-b", "content": "parent b", "children": []},
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_prepend(mock_ctx, page="Project Alpha", blocks=payload)
    data = json.loads(result)

    assert [method for method, _ in calls[1:5]] == [
        "logseq.Editor.prependBlockInPage",
        "logseq.Editor.insertBatchBlock",
        "logseq.Editor.insertBatchBlock",
        "logseq.Editor.getPageBlocksTree",
    ]
    assert calls[2][1][0] == "uuid-parent-a"
    assert calls[2][1][2] == {"sibling": False}
    assert calls[3][1][0] == "uuid-parent-a"
    assert calls[3][1][2] == {"sibling": True}
    assert data["prepended"] == 4
    assert [block["content"] for block in data["blocks"]] == ["parent a", "parent b"]
    assert [child["content"] for child in data["blocks"][0]["children"]] == ["child a1", "child a2"]


async def test_block_update_changes_content_and_verifies_readback(token_env):
    from logseq_mcp.tools.write import block_update

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getBlock":
            return {"id": 9, "uuid": "block-uuid", "content": "new content", "children": []}
        if method == "logseq.Editor.updateBlock":
            return {"uuid": "block-uuid"}
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_update(mock_ctx, uuid="block-uuid", content="new content")
    data = json.loads(result)

    assert [method for method, _ in calls] == [
        "logseq.Editor.updateBlock",
        "logseq.Editor.getBlock",
    ]
    assert data["uuid"] == "block-uuid"
    assert data["content"] == "new content"


async def test_block_update_tolerates_null_rpc_response_when_readback_matches(token_env):
    from logseq_mcp.tools.write import block_update

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getBlock":
            return {"id": 9, "uuid": "block-uuid", "content": "new content", "children": []}
        if method == "logseq.Editor.updateBlock":
            return None
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_update(mock_ctx, uuid="block-uuid", content="new content")
    data = json.loads(result)

    assert [method for method, _ in calls] == [
        "logseq.Editor.updateBlock",
        "logseq.Editor.getBlock",
    ]
    assert data == {"uuid": "block-uuid", "content": "new content"}


async def test_block_delete_removes_block_from_followup_reads(token_env):
    from logseq_mcp.tools.write import block_delete

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getBlock":
            return {
                "id": 9,
                "uuid": "block-uuid",
                "content": "to delete",
                "page": {"id": 2, "uuid": "page-uuid", "name": "Project Alpha"},
                "children": [],
            }
        if method == "logseq.Editor.removeBlock":
            return True
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_delete(mock_ctx, uuid="block-uuid")
    data = json.loads(result)

    assert [method for method, _ in calls] == [
        "logseq.Editor.getBlock",
        "logseq.Editor.removeBlock",
    ]
    assert data == {"ok": True, "uuid": "block-uuid"}


async def test_block_delete_tolerates_null_rpc_response_when_block_disappears(token_env):
    from logseq_mcp.tools.write import block_delete

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getBlock":
            return {
                "id": 9,
                "uuid": "block-uuid",
                "content": "to delete",
                "page": {"id": 2, "uuid": "page-uuid", "name": "Project Alpha"},
                "children": [],
            }
        if method == "logseq.Editor.removeBlock":
            return None
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_delete(mock_ctx, uuid="block-uuid")
    data = json.loads(result)

    assert [method for method, _ in calls] == [
        "logseq.Editor.getBlock",
        "logseq.Editor.removeBlock",
    ]
    assert data == {"ok": True, "uuid": "block-uuid"}


async def test_block_update_unchanged_readback_logs_warning_but_returns_actual(token_env):
    from logseq_mcp.tools.write import block_update

    async def fake_call(method, *args):
        if method == "logseq.Editor.getBlock":
            return {"id": 9, "uuid": "block-uuid", "content": "old content", "children": []}
        if method == "logseq.Editor.updateBlock":
            return {"uuid": "block-uuid"}
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await block_update(mock_ctx, uuid="block-uuid", content="new content")
    data = json.loads(result)

    assert data == {"uuid": "block-uuid", "content": "old content"}


async def test_invalid_nested_payload_fails_before_first_write(token_env):
    from logseq_mcp.tools.write import block_append

    client = AsyncMock()
    mock_ctx = _make_ctx(client)

    with pytest.raises((McpError, ValueError, TypeError)):
        await block_append(
            mock_ctx,
            page="Project Alpha",
            blocks=[{"content": "root", "children": ["invalid-child"]}],
        )

    client._call.assert_not_called()


async def test_journal_append_invalid_date_fails_before_first_write(token_env):
    from logseq_mcp.tools.write import journal_append

    client = AsyncMock()
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="invalid journal date"):
        await journal_append(mock_ctx, date="2026-02-30", blocks=["root 1"])

    client._call.assert_not_called()


async def test_journal_append_invalid_nested_payload_fails_before_first_write(token_env):
    from logseq_mcp.tools.write import journal_append

    client = AsyncMock()
    mock_ctx = _make_ctx(client)

    with pytest.raises((McpError, ValueError, TypeError)):
        await journal_append(
            mock_ctx,
            date="2026-04-13",
            blocks=[{"content": "root", "children": ["invalid-child"]}],
        )

    client._call.assert_not_called()


async def test_block_append_missing_page_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import block_append

    client = AsyncMock()
    client._call = AsyncMock(return_value=None)
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="page not found"):
        await block_append(mock_ctx, page="Missing Page", blocks=["root 1"])


async def test_block_update_missing_uuid_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import block_update

    client = AsyncMock()
    client._call = AsyncMock(return_value=None)
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="block not found"):
        await block_update(mock_ctx, uuid="missing-uuid", content="updated")


async def test_block_delete_missing_uuid_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import block_delete

    client = AsyncMock()
    client._call = AsyncMock(return_value=None)
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="block not found"):
        await block_delete(mock_ctx, uuid="missing-uuid")


async def test_delete_page_removes_page_from_followup_reads(token_env):
    from logseq_mcp.tools.write import delete_page

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method != "logseq.Editor.getPage":
            return None
        if len(calls) == 1:
            return {
                "id": 1,
                "uuid": "page-uuid",
                "name": "Phase 05 Lifecycle/Delete Me",
                "original-name": "Phase 05 Lifecycle/Delete Me",
                "journal?": False,
            }
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    payload = json.loads(await delete_page(mock_ctx, "Phase 05 Lifecycle/Delete Me"))

    assert [method for method, _ in calls] == [
        "logseq.Editor.getPage",
        "logseq.Editor.deletePage",
    ]
    assert payload == {"ok": True, "name": "Phase 05 Lifecycle/Delete Me"}


async def test_delete_page_missing_page_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import delete_page

    client = AsyncMock()
    client._call = AsyncMock(return_value=None)
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="page not found: Missing Lifecycle Page"):
        await delete_page(mock_ctx, "Missing Lifecycle Page")


async def test_rename_page_moves_resolution_to_new_name(token_env):
    from logseq_mcp.tools.write import rename_page

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            page_name = args[0]
            if page_name == "Phase 05 Lifecycle/Rename Source":
                return None
            if page_name == "Phase 05 Lifecycle/Rename Target":
                if len(calls) == 1:
                    return None
                return {
                    "id": 10,
                    "uuid": "page-old",
                    "name": page_name,
                    "original-name": page_name,
                    "journal?": False,
                }
        if method == "logseq.Editor.renamePage":
            return None
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    payload = json.loads(
        await rename_page(mock_ctx, "Phase 05 Lifecycle/Rename Source", "Phase 05 Lifecycle/Rename Target")
    )
    assert [method for method, _ in calls] == [
        "logseq.Editor.getPage",
        "logseq.Editor.renamePage",
        "logseq.Editor.getPage",
    ]
    assert payload == {
        "ok": True,
        "old_name": "Phase 05 Lifecycle/Rename Source",
        "new_name": "Phase 05 Lifecycle/Rename Target",
    }


async def test_rename_page_existing_destination_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import rename_page

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method != "logseq.Editor.getPage":
            return None
        page_name = args[0]
        if page_name == "Phase 05 Lifecycle/Rename Target":
            return {
                "id": 11,
                "uuid": "page-new",
                "name": page_name,
                "original-name": page_name,
                "journal?": False,
            }
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="page already exists: Phase 05 Lifecycle/Rename Target"):
        await rename_page(mock_ctx, "Phase 05 Lifecycle/Rename Source", "Phase 05 Lifecycle/Rename Target")

    assert [method for method, _ in calls] == [
        "logseq.Editor.getPage",
    ]


async def test_rename_page_missing_source_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import rename_page

    client = AsyncMock()
    client._call = AsyncMock(return_value=None)
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="page not found"):
        await rename_page(mock_ctx, "Missing Rename Source", "Any Target")


async def test_rename_page_same_name_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import rename_page

    client = AsyncMock()
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="new_name must differ from old_name"):
        await rename_page(mock_ctx, "Phase 05 Lifecycle/Same", "Phase 05 Lifecycle/Same")

    client._call.assert_not_called()


async def test_lifecycle_tools_preserve_namespaced_page_names(token_env):
    from logseq_mcp.tools.write import rename_page

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            page_name = args[0]
            if page_name == "Phase 05 Namespace/Source":
                return None
            if page_name == "Phase 05 Namespace/Renamed":
                if len(calls) == 1:
                    return None
                return {
                    "id": 20,
                    "uuid": "page-ns",
                    "name": page_name.lower(),
                    "original-name": page_name,
                    "namespace": {"id": 7},
                    "journal?": False,
                }
        if method == "logseq.Editor.renamePage":
            return None
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    payload = json.loads(await rename_page(mock_ctx, "Phase 05 Namespace/Source", "Phase 05 Namespace/Renamed"))

    assert payload == {
        "ok": True,
        "old_name": "Phase 05 Namespace/Source",
        "new_name": "Phase 05 Namespace/Renamed",
    }
    assert [method for method, _ in calls] == [
        "logseq.Editor.getPage",
        "logseq.Editor.renamePage",
        "logseq.Editor.getPage",
    ]


async def test_move_block_before_verifies_relative_placement_and_subtree(token_env):
    from logseq_mcp.tools.write import move_block

    calls = []
    source_block = {
        "id": 100,
        "uuid": "move-root",
        "content": "move root",
        "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
        "children": [
            {"id": 101, "uuid": "move-child", "content": "move child", "children": []},
            {"id": 102, "uuid": "move-grandchild", "content": "move grandchild", "children": []},
        ],
    }
    target_block = {
        "id": 200,
        "uuid": "target-root",
        "content": "target root",
        "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
        "children": [],
    }

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getBlock":
            block_uuid = args[0]
            if block_uuid == "move-root":
                return source_block
            if block_uuid == "target-root":
                return target_block
        if method == "logseq.Editor.moveBlock":
            return {"uuid": "move-root"}
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {
                    "id": 100,
                    "uuid": "move-root",
                    "content": "move root",
                    "children": [
                        {"id": 101, "uuid": "move-child", "content": "move child", "children": []},
                        {"id": 102, "uuid": "move-grandchild", "content": "move grandchild", "children": []},
                    ],
                },
                {"id": 200, "uuid": "target-root", "content": "target root", "children": []},
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position="before")
    data = json.loads(result)

    assert [method for method, _ in calls] == [
        "logseq.Editor.getBlock",
        "logseq.Editor.getBlock",
        "logseq.Editor.moveBlock",
        "logseq.Editor.getPageBlocksTree",
    ]
    assert calls[2] == ("logseq.Editor.moveBlock", ("move-root", "target-root", {"before": True}))
    assert data == {
        "ok": True,
        "uuid": "move-root",
        "target_uuid": "target-root",
        "position": "before",
    }


async def test_move_block_after_verifies_relative_placement_and_subtree(token_env):
    from logseq_mcp.tools.write import move_block

    async def fake_call(method, *args):
        if method == "logseq.Editor.getBlock":
            if args[0] == "move-root":
                return {
                    "id": 100,
                    "uuid": "move-root",
                    "content": "move root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [{"id": 101, "uuid": "move-child", "content": "move child", "children": []}],
                }
            if args[0] == "target-root":
                return {
                    "id": 200,
                    "uuid": "target-root",
                    "content": "target root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [],
                }
        if method == "logseq.Editor.moveBlock":
            return True
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {"id": 200, "uuid": "target-root", "content": "target root", "children": []},
                {
                    "id": 100,
                    "uuid": "move-root",
                    "content": "move root",
                    "children": [{"id": 101, "uuid": "move-child", "content": "move child", "children": []}],
                },
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position="after")
    data = json.loads(result)

    assert data == {
        "ok": True,
        "uuid": "move-root",
        "target_uuid": "target-root",
        "position": "after",
    }


async def test_move_block_child_verifies_relative_placement_and_subtree(token_env):
    from logseq_mcp.tools.write import move_block

    async def fake_call(method, *args):
        if method == "logseq.Editor.getBlock":
            if args[0] == "move-root":
                return {
                    "id": 100,
                    "uuid": "move-root",
                    "content": "move root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [{"id": 101, "uuid": "move-child", "content": "move child", "children": []}],
                }
            if args[0] == "target-root":
                return {
                    "id": 200,
                    "uuid": "target-root",
                    "content": "target root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [
                        {"id": 201, "uuid": "existing-child", "content": "existing child", "children": []},
                        {
                            "id": 100,
                            "uuid": "move-root",
                            "content": "move root",
                            "children": [{"id": 101, "uuid": "move-child", "content": "move child", "children": []}],
                        },
                    ],
                }
        if method == "logseq.Editor.moveBlock":
            return None
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {
                    "id": 200,
                    "uuid": "target-root",
                    "content": "target root",
                    "children": [
                        {"id": 201, "uuid": "existing-child", "content": "existing child", "children": []},
                        {
                            "id": 100,
                            "uuid": "move-root",
                            "content": "move root",
                            "children": [{"id": 101, "uuid": "move-child", "content": "move child", "children": []}],
                        },
                    ],
                }
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position="child")
    data = json.loads(result)

    assert data == {
        "ok": True,
        "uuid": "move-root",
        "target_uuid": "target-root",
        "position": "child",
    }


async def test_move_block_cross_page_reads_destination_tree_and_checks_source_absence(token_env):
    from logseq_mcp.tools.write import move_block

    calls = []
    source_block = {
        "id": 100,
        "uuid": "move-root",
        "content": "move root",
        "page": {"id": 1, "uuid": "source-page-uuid", "name": "Source Page"},
        "children": [{"id": 101, "uuid": "move-child", "content": "move child", "children": []}],
    }
    target_block = {
        "id": 200,
        "uuid": "target-root",
        "content": "target root",
        "page": {"id": 2, "uuid": "target-page-uuid", "name": "Target Page"},
        "children": [],
    }

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getBlock":
            if args[0] == "move-root":
                return source_block
            if args[0] == "target-root":
                return target_block
        if method == "logseq.Editor.moveBlock":
            return {"uuid": "move-root"}
        if method == "logseq.Editor.getPageBlocksTree":
            page_name = args[0]
            if page_name == "Target Page":
                return [
                    {"id": 200, "uuid": "target-root", "content": "target root", "children": []},
                    {
                        "id": 100,
                        "uuid": "move-root",
                        "content": "move root",
                        "children": [{"id": 101, "uuid": "move-child", "content": "move child", "children": []}],
                    },
                ]
            if page_name == "Source Page":
                return [{"id": 300, "uuid": "source-sibling", "content": "source sibling", "children": []}]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position="after")
    data = json.loads(result)

    assert data == {
        "ok": True,
        "uuid": "move-root",
        "target_uuid": "target-root",
        "position": "after",
    }
    assert [method for method, _ in calls] == [
        "logseq.Editor.getBlock",
        "logseq.Editor.getBlock",
        "logseq.Editor.moveBlock",
        "logseq.Editor.getPageBlocksTree",
    ]
    assert calls[3] == ("logseq.Editor.getPageBlocksTree", ("Target Page",))


@pytest.mark.parametrize("position", ["", "left", "sibling"])
async def test_move_block_rejects_invalid_position(token_env, position):
    from logseq_mcp.tools.write import move_block

    mock_ctx = _make_ctx()

    with pytest.raises(McpError, match="position must be one of: after, before, child"):
        await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position=position)


async def test_move_block_fails_when_source_block_missing(token_env):
    from logseq_mcp.tools.write import move_block

    async def fake_call(method, *args):
        if method == "logseq.Editor.getBlock":
            return None
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="block not found: move-root"):
        await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position="before")


async def test_move_block_fails_when_target_block_missing(token_env):
    from logseq_mcp.tools.write import move_block

    async def fake_call(method, *args):
        if method == "logseq.Editor.getBlock":
            if args[0] == "move-root":
                return {
                    "id": 100,
                    "uuid": "move-root",
                    "content": "move root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [],
                }
            return None
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="block not found: target-root"):
        await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position="before")


async def test_move_block_fails_when_subtree_descendants_are_lost(token_env):
    from logseq_mcp.tools.write import move_block

    async def fake_call(method, *args):
        if method == "logseq.Editor.getBlock":
            if args[0] == "move-root":
                return {
                    "id": 100,
                    "uuid": "move-root",
                    "content": "move root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [{"id": 101, "uuid": "move-child", "content": "move child", "children": []}],
                }
            if args[0] == "target-root":
                return {
                    "id": 200,
                    "uuid": "target-root",
                    "content": "target root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [],
                }
        if method == "logseq.Editor.moveBlock":
            return True
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {"id": 100, "uuid": "move-root", "content": "move root", "children": []},
                {"id": 200, "uuid": "target-root", "content": "target root", "children": []},
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="moved subtree lost descendants after move: move-root"):
        await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position="before")


async def test_move_block_fails_when_readback_does_not_match_requested_position(token_env):
    from logseq_mcp.tools.write import move_block

    async def fake_call(method, *args):
        if method == "logseq.Editor.getBlock":
            if args[0] == "move-root":
                return {
                    "id": 100,
                    "uuid": "move-root",
                    "content": "move root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [],
                }
            if args[0] == "target-root":
                return {
                    "id": 200,
                    "uuid": "target-root",
                    "content": "target root",
                    "page": {"id": 1, "uuid": "page-uuid", "name": "Sandbox"},
                    "children": [],
                }
        if method == "logseq.Editor.moveBlock":
            return True
        if method == "logseq.Editor.getPageBlocksTree":
            return [
                {"id": 200, "uuid": "target-root", "content": "target root", "children": []},
                {"id": 100, "uuid": "move-root", "content": "move root", "children": []},
            ]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="move verification failed for block: move-root"):
        await move_block(mock_ctx, uuid="move-root", target_uuid="target-root", position="before")


def test_resolve_journal_page_name_accepts_iso_dates():
    from logseq_mcp.tools.write import _resolve_journal_page_name

    assert _resolve_journal_page_name("2026-03-12") == "2026-03-12"


@pytest.mark.parametrize("value", ["2026-13-12", "2026-02-30", "03-12-2026", "not-a-date"])
def test_resolve_journal_page_name_rejects_invalid_dates(value):
    from logseq_mcp.tools.write import _resolve_journal_page_name

    with pytest.raises(McpError, match=f"invalid journal date: {value}"):
        _resolve_journal_page_name(value)


def test_resolve_journal_page_name_rejects_unsupported_format_assumptions():
    from logseq_mcp.tools.write import _resolve_journal_page_name

    with pytest.raises(McpError, match="unsupported journal page title format: MMM do, yyyy"):
        _resolve_journal_page_name("2026-03-12", page_title_format="MMM do, yyyy")


async def test_journal_today_creates_missing_page_and_reads_back_journal_payload(token_env, monkeypatch):
    from datetime import date

    from logseq_mcp.tools import write as write_tools

    calls = []

    async def fake_call(method, *args):
        calls.append((method, args))
        if method == "logseq.Editor.getPage":
            return None
        if method == "logseq.Editor.createPage":
            return {
                "id": 30,
                "uuid": "journal-page-uuid",
                "name": "2026-03-12",
                "original-name": "2026-03-12",
                "journal?": True,
                "journal-day": 20260312,
            }
        if method == "logseq.Editor.getPageBlocksTree":
            return []
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)
    monkeypatch.setattr(write_tools, "_today_date", lambda: date(2026, 3, 12))

    payload = json.loads(await write_tools.journal_today(mock_ctx))

    assert [method for method, _ in calls] == [
        "logseq.Editor.getPage",
        "logseq.Editor.createPage",
        "logseq.Editor.getPageBlocksTree",
    ]
    assert calls[1] == (
        "logseq.Editor.createPage",
        ("2026-03-12", {}, {"createFirstBlock": False}),
    )
    assert payload == {
        "page": {
            "id": 30,
            "uuid": "journal-page-uuid",
            "name": "2026-03-12",
            "original_name": "2026-03-12",
            "journal": True,
            "journal_day": 20260312,
            "properties": {},
            "namespace": "",
        },
        "created": True,
        "blocks": [],
        "block_count": 0,
    }


# ---------------------------------------------------------------------------
# JOUR-03: journal_range unit coverage
# ---------------------------------------------------------------------------


def _make_journal_page(date_str: str) -> dict:
    """Build a minimal journal PageEntity dict for a given ISO date string."""
    day_int = int(date_str.replace("-", ""))
    return {
        "id": day_int,
        "uuid": f"journal-{date_str}",
        "name": date_str,
        "original-name": date_str,
        "journal?": True,
        "journal-day": day_int,
    }


def _make_non_journal_page(name: str) -> dict:
    return {
        "id": 999,
        "uuid": f"page-{name}",
        "name": name,
        "original-name": name,
        "journal?": False,
        "journal-day": None,
    }


async def test_journal_range_single_day_returns_one_entry(token_env):
    from logseq_mcp.tools.write import journal_range

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPage":
            return _make_journal_page("2030-06-15")
        if method == "logseq.Editor.getPageBlocksTree":
            return [{"id": 1, "uuid": "blk-1", "content": "entry", "children": []}]
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = json.loads(await journal_range(mock_ctx, start_date="2030-06-15", end_date="2030-06-15"))

    assert result["start_date"] == "2030-06-15"
    assert result["end_date"] == "2030-06-15"
    assert result["days"] == 1
    assert result["entry_count"] == 1
    assert len(result["entries"]) == 1
    assert result["entries"][0]["page"]["name"] == "2030-06-15"


async def test_journal_range_multi_day_inclusive_returns_all_existing(token_env):
    from logseq_mcp.tools.write import journal_range

    existing_dates = {"2030-06-10", "2030-06-11", "2030-06-12"}

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPage":
            date_str = args[0]
            if date_str in existing_dates:
                return _make_journal_page(date_str)
            return None
        if method == "logseq.Editor.getPageBlocksTree":
            return []
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = json.loads(await journal_range(mock_ctx, start_date="2030-06-10", end_date="2030-06-12"))

    assert result["days"] == 3
    assert result["entry_count"] == 3
    returned_dates = [e["page"]["name"] for e in result["entries"]]
    assert returned_dates == sorted(returned_dates)


async def test_journal_range_missing_days_are_skipped(token_env):
    from logseq_mcp.tools.write import journal_range

    existing_dates = {"2030-07-01", "2030-07-03"}

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPage":
            date_str = args[0]
            if date_str in existing_dates:
                return _make_journal_page(date_str)
            return None
        if method == "logseq.Editor.getPageBlocksTree":
            return []
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = json.loads(await journal_range(mock_ctx, start_date="2030-07-01", end_date="2030-07-03"))

    assert result["days"] == 3
    assert result["entry_count"] == 2
    returned_dates = [e["page"]["name"] for e in result["entries"]]
    assert returned_dates == ["2030-07-01", "2030-07-03"]


async def test_journal_range_entries_are_sorted_ascending(token_env):
    from logseq_mcp.tools.write import journal_range

    dates_in_range = ["2030-08-01", "2030-08-02", "2030-08-03"]

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPage":
            date_str = args[0]
            return _make_journal_page(date_str)
        if method == "logseq.Editor.getPageBlocksTree":
            return []
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = json.loads(await journal_range(mock_ctx, start_date="2030-08-01", end_date="2030-08-03"))

    returned_dates = [e["page"]["name"] for e in result["entries"]]
    assert returned_dates == sorted(returned_dates)
    assert returned_dates == dates_in_range


async def test_journal_range_invalid_start_date_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import journal_range

    client = AsyncMock()
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="invalid start_date"):
        await journal_range(mock_ctx, start_date="not-a-date", end_date="2030-06-15")

    client._call.assert_not_called()


async def test_journal_range_invalid_end_date_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import journal_range

    client = AsyncMock()
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="invalid end_date"):
        await journal_range(mock_ctx, start_date="2030-06-01", end_date="2030-13-01")

    client._call.assert_not_called()


async def test_journal_range_reversed_range_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import journal_range

    client = AsyncMock()
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="start_date must be on or before end_date"):
        await journal_range(mock_ctx, start_date="2030-06-15", end_date="2030-06-01")

    client._call.assert_not_called()


async def test_journal_range_span_exceeding_max_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import journal_range

    client = AsyncMock()
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="journal range exceeds maximum span"):
        await journal_range(mock_ctx, start_date="2030-01-01", end_date="2031-02-10")

    client._call.assert_not_called()


async def test_journal_range_non_journal_page_raises_explicit_error(token_env):
    from logseq_mcp.tools.write import journal_range

    async def fake_call(method, *args):
        if method == "logseq.Editor.getPage":
            return _make_non_journal_page("2030-09-01")
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    with pytest.raises(McpError, match="resolved page is not a journal page"):
        await journal_range(mock_ctx, start_date="2030-09-01", end_date="2030-09-01")


async def test_journal_range_does_not_call_get_all_pages(token_env):
    from logseq_mcp.tools.write import journal_range

    async def fake_call(method, *args):
        if method == "logseq.Editor.getAllPages":
            raise AssertionError("getAllPages must not be called by journal_range")
        if method == "logseq.Editor.getPage":
            return _make_journal_page(args[0])
        if method == "logseq.Editor.getPageBlocksTree":
            return []
        return None

    client = AsyncMock()
    client._call = fake_call
    mock_ctx = _make_ctx(client)

    result = json.loads(await journal_range(mock_ctx, start_date="2030-10-01", end_date="2030-10-02"))

    assert result["entry_count"] == 2
