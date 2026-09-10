import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta

from mcp import McpError
from mcp.server.fastmcp import Context
from mcp.types import ErrorData, INTERNAL_ERROR
from pydantic import BaseModel, Field, ValidationError, model_validator

from logseq_mcp.server import AppContext, mcp
from logseq_mcp.types import BlockEntity, PageEntity

logger = logging.getLogger(__name__)

SUPPORTED_JOURNAL_PAGE_TITLE_FORMAT = "yyyy-MM-dd"


class WriteBlockInput(BaseModel):
    content: str
    properties: dict = Field(default_factory=dict)
    children: list["WriteBlockInput"] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def validate_node(cls, value):
        if not isinstance(value, dict):
            raise TypeError("block payload must be an object")

        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("block content must be a non-empty string")

        properties = value.get("properties", {})
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            raise TypeError("block properties must be a dict")

        children = value.get("children", [])
        if children is None:
            children = []
        if not isinstance(children, list):
            raise TypeError("block children must be a list")
        for child in children:
            if not isinstance(child, dict):
                raise TypeError("nested block children must be objects")

        normalized = dict(value)
        normalized["content"] = content
        normalized["properties"] = properties
        normalized["children"] = children
        return normalized


WriteBlockInput.model_rebuild()


def _count_blocks(blocks: list[BlockEntity]) -> int:
    total = 0
    for block in blocks:
        total += 1 + _count_blocks(block.children)
    return total


def _parse_block_tree(raw_blocks: list) -> list[BlockEntity]:
    parsed: list[BlockEntity] = []
    for raw in raw_blocks:
        parsed.append(BlockEntity.model_validate(raw))
    return parsed


def _contains_block_uuid(blocks: list[BlockEntity], uuid: str) -> bool:
    for block in blocks:
        if block.uuid == uuid or _contains_block_uuid(block.children, uuid):
            return True
    return False


def _collect_subtree_uuids(block: BlockEntity) -> set[str]:
    uuids = {block.uuid}
    for child in block.children:
        uuids.update(_collect_subtree_uuids(child))
    return uuids


def _find_block_with_parent(
    blocks: list[BlockEntity],
    uuid: str,
    parent_uuid: str | None = None,
) -> tuple[BlockEntity, str | None, list[BlockEntity]] | None:
    for siblings in (blocks,):
        for index, block in enumerate(siblings):
            if block.uuid == uuid:
                return block, parent_uuid, siblings
            nested = _find_block_with_parent(block.children, uuid, block.uuid)
            if nested is not None:
                return nested
    return None


def _validate_move_position(position: str) -> str:
    if position not in {"before", "after", "child"}:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="position must be one of: after, before, child"))
    return position


def _today_date() -> date:
    override = os.environ.get("LOGSEQ_MCP_TEST_TODAY", "").strip()
    if override:
        try:
            return date.fromisoformat(override)
        except ValueError as exc:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"invalid LOGSEQ_MCP_TEST_TODAY: {override}")) from exc
    return datetime.now().date()


def _resolve_journal_page_name(
    date_str: str | None = None,
    *,
    page_title_format: str = SUPPORTED_JOURNAL_PAGE_TITLE_FORMAT,
) -> str:
    if page_title_format != SUPPORTED_JOURNAL_PAGE_TITLE_FORMAT:
        raise McpError(
            ErrorData(
                code=INTERNAL_ERROR,
                message=f"unsupported journal page title format: {page_title_format}",
            )
        )

    if date_str is None:
        journal_date = _today_date()
    else:
        try:
            journal_date = date.fromisoformat(date_str)
        except ValueError as exc:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"invalid journal date: {date_str}")) from exc

    return journal_date.isoformat()


def _move_block_options(position: str) -> dict:
    if position == "before":
        return {"before": True}
    if position == "child":
        return {"children": True}
    return {}


def _verify_block_moved(
    block_tree: list[BlockEntity],
    *,
    moved_uuid: str,
    target_uuid: str,
    position: str,
    subtree_uuids: set[str],
) -> None:
    matches = [uuid for uuid in subtree_uuids if _contains_block_uuid(block_tree, uuid)]
    if len(matches) != len(subtree_uuids):
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message=f"moved subtree lost descendants after move: {moved_uuid}")
        )

    moved_result = _find_block_with_parent(block_tree, moved_uuid)
    target_result = _find_block_with_parent(block_tree, target_uuid)
    if moved_result is None or target_result is None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"move verification failed for block: {moved_uuid}"))

    moved_block, moved_parent_uuid, moved_siblings = moved_result
    _target_block, target_parent_uuid, target_siblings = target_result

    if position == "child":
        if moved_parent_uuid != target_uuid:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"move verification failed for block: {moved_uuid}"))
        return

    if moved_parent_uuid != target_parent_uuid or moved_siblings is not target_siblings:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"move verification failed for block: {moved_uuid}"))

    moved_index = moved_siblings.index(moved_block)
    target_index = target_siblings.index(_target_block)
    if position == "before" and moved_index >= target_index:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"move verification failed for block: {moved_uuid}"))
    if position == "after" and moved_index <= target_index:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"move verification failed for block: {moved_uuid}"))


def _normalize_blocks(blocks: None | str | dict | list) -> list[WriteBlockInput]:
    if blocks is None:
        return []

    raw_nodes = blocks if isinstance(blocks, list) else [blocks]
    normalized: list[WriteBlockInput] = []

    for raw_node in raw_nodes:
        if isinstance(raw_node, str):
            normalized.append(WriteBlockInput(content=raw_node))
            continue

        if not isinstance(raw_node, dict):
            raise TypeError("blocks must be strings, objects, or lists of them")

        normalized.append(WriteBlockInput.model_validate(raw_node))

    return normalized


def _mutation_options(block: WriteBlockInput) -> dict:
    opts: dict = {}
    if block.properties:
        opts["properties"] = block.properties
    return opts


def _extract_uuid(raw_result, method: str) -> str:
    if not isinstance(raw_result, dict):
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"{method} returned an invalid response"))

    uuid = raw_result.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"{method} did not return a block uuid"))

    return uuid


def _to_batch_block(block: WriteBlockInput) -> dict:
    """Convert a WriteBlockInput to Logseq's IBatchBlock dict structure."""
    node: dict = {"content": block.content}
    if block.properties:
        node["properties"] = block.properties
    if block.children:
        node["children"] = [_to_batch_block(child) for child in block.children]
    return node


def _count_batch_nodes(nodes: list[dict]) -> int:
    """Count all nodes in a batch structure recursively."""
    total = 0
    for node in nodes:
        total += 1
        total += _count_batch_nodes(node.get("children", []))
    return total


async def _append_tree_to_page(
    client, page_name: str, blocks: list[WriteBlockInput], *, prepend: bool = False
) -> int:
    """Insert a block tree into a page using batch insertion (max 2-3 RPCs).

    The first root block is anchored via ``appendBlockInPage`` (or
    ``prependBlockInPage`` when *prepend* is set) to obtain a UUID, then
    ``insertBatchBlock`` inserts the remainder — the first block's children and
    any sibling roots — in at most two batch calls. This replaces the former
    per-node recursive approach that issued one RPC per block, eliminating
    network-roundtrip stutter on large writes.
    """
    if not blocks:
        return 0

    anchor_method = (
        "logseq.Editor.prependBlockInPage" if prepend else "logseq.Editor.appendBlockInPage"
    )
    first = blocks[0]
    result = await client._call(
        anchor_method,
        page_name,
        first.content,
        _mutation_options(first),
    )
    anchor_uuid = _extract_uuid(result, anchor_method)
    appended = 1

    if first.children:
        batch = [_to_batch_block(child) for child in first.children]
        await client._call(
            "logseq.Editor.insertBatchBlock",
            anchor_uuid,
            batch,
            {"sibling": False},
        )
        appended += _count_batch_nodes(batch)

    if len(blocks) > 1:
        batch = [_to_batch_block(block) for block in blocks[1:]]
        await client._call(
            "logseq.Editor.insertBatchBlock",
            anchor_uuid,
            batch,
            {"sibling": True},
        )
        appended += _count_batch_nodes(batch)

    return appended


async def _get_page_or_error(client, page_name: str) -> PageEntity:
    raw = await client._call("logseq.Editor.getPage", page_name)
    if raw is None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"page not found: {page_name}"))
    return PageEntity.model_validate(raw)


async def _get_page_or_none(client, page_name: str) -> PageEntity | None:
    raw = await client._call("logseq.Editor.getPage", page_name)
    if raw is None:
        return None

    try:
        return PageEntity.model_validate(raw)
    except ValidationError as exc:
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message=f"logseq.Editor.getPage returned an invalid response: {exc}")
        ) from exc


async def _verify_page_present(client, page_name: str) -> PageEntity:
    page = await _get_page_or_none(client, page_name)
    if page is None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"page not found: {page_name}"))
    return page


async def _verify_page_absent(client, page_name: str) -> None:
    page = await _get_page_or_none(client, page_name)
    if page is not None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"page still exists after delete: {page_name}"))


async def _ensure_journal_page(client, page_name: str) -> tuple[PageEntity, bool, list[BlockEntity]]:
    page = await _get_page_or_none(client, page_name)
    created = False

    if page is None:
        created_page = await client._call(
            "logseq.Editor.createPage",
            page_name,
            {},
            {"createFirstBlock": False},
        )
        if created_page is None:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"failed to create journal page: {page_name}"))
        created = True
        try:
            page = PageEntity.model_validate(created_page)
        except Exception:
            page = await _verify_page_present(client, page_name)

    if not page.journal:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"resolved page is not a journal page: {page_name}"))

    block_tree = await _get_page_blocks(client, page_name)
    return page, created, block_tree


def _page_matches_name(page: PageEntity, expected_name: str) -> bool:
    if page.name.casefold() == expected_name.casefold():
        return True
    if isinstance(page.original_name, str) and page.original_name == expected_name:
        return True
    return False


def _validate_rename_target(old_name: str, new_name: str) -> None:
    if not isinstance(old_name, str) or not old_name.strip():
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="old_name must be a non-empty string"))
    if not isinstance(new_name, str) or not new_name.strip():
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="new_name must be a non-empty string"))
    if old_name == new_name:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="new_name must differ from old_name"))


async def _verify_rename_target_available(client, new_name: str) -> None:
    page = await _get_page_or_none(client, new_name)
    if page is not None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"page already exists: {new_name}"))


async def _verify_rename_readback(client, old_name: str, new_name: str) -> PageEntity:
    await _verify_page_absent(client, old_name)
    page = await _verify_page_present(client, new_name)
    if not _page_matches_name(page, new_name):
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message=f"renamed page did not resolve at new name: {new_name}")
        )
    return page


async def _get_page_blocks(client, page_name: str) -> list[BlockEntity]:
    raw = await client._call("logseq.Editor.getPageBlocksTree", page_name)
    if not isinstance(raw, list):
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"page block tree unavailable: {page_name}"))
    return _parse_block_tree(raw)


async def _get_block_or_error(client, uuid: str) -> BlockEntity:
    raw = await client._call("logseq.Editor.getBlock", uuid, {"includeChildren": True})
    if raw is None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"block not found: {uuid}"))

    try:
        return BlockEntity.model_validate(raw)
    except ValidationError as exc:
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message=f"logseq.Editor.getBlock returned an invalid response: {exc}")
        ) from exc


async def _verify_block_readback(client, uuid: str, expected_content: str) -> BlockEntity:
    block = await _get_block_or_error(client, uuid)
    if block.content != expected_content:
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message=f"updated content did not match readback for block: {uuid}")
        )
    return block


async def _verify_block_absent(client, uuid: str, page_name: str | None = None) -> None:
    raw = await client._call("logseq.Editor.getBlock", uuid, {"includeChildren": True})
    if raw is not None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"block still exists after delete: {uuid}"))

    if page_name:
        block_tree = await _get_page_blocks(client, page_name)
        if _contains_block_uuid(block_tree, uuid):
            raise McpError(
                ErrorData(code=INTERNAL_ERROR, message=f"block still present in page tree after delete: {uuid}")
            )


async def _verify_block_move_readback(
    client,
    *,
    moved_uuid: str,
    target_uuid: str,
    position: str,
    destination_page_name: str,
    source_page_name: str | None,
    subtree_uuids: set[str],
) -> None:
    destination_tree = await _get_page_blocks(client, destination_page_name)
    _verify_block_moved(
        destination_tree,
        moved_uuid=moved_uuid,
        target_uuid=target_uuid,
        position=position,
        subtree_uuids=subtree_uuids,
    )

    if source_page_name and source_page_name != destination_page_name:
        source_tree = await _get_page_blocks(client, source_page_name)
        if _contains_block_uuid(source_tree, moved_uuid):
            raise McpError(
                ErrorData(
                    code=INTERNAL_ERROR,
                    message=f"moved block still present on source page after move: {moved_uuid}",
                )
            )


def _normalize_error(exc: Exception) -> McpError:
    if isinstance(exc, McpError):
        return exc
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return McpError(ErrorData(code=INTERNAL_ERROR, message=str(exc)))
    raise exc


@mcp.tool()
async def page_create(
    ctx: Context,
    name: str,
    properties: dict | None = None,
    blocks: list | None = None,
) -> str:
    """Create a new Logseq page with optional properties and initial blocks.

    Pass `name` with natural casing and orthography (for example "Meeting Notes
    2026" or "Projekt Ideen"). Logseq resolves page names case-insensitively, so
    casing only affects how the page is displayed; it preserves the supplied
    casing as the page's display name. Do NOT lowercase or slugify `name` — that
    would make the page show up in lowercase in Logseq.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("page_create: %s", name)

    try:
        normalized_blocks = _normalize_blocks(blocks)
    except Exception as exc:
        raise _normalize_error(exc)

    page_properties = properties or {}
    if not isinstance(page_properties, dict):
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="page properties must be a dict"))

    created = await client._call(
        "logseq.Editor.createPage",
        name,
        page_properties,
        {"createFirstBlock": False},
    )
    if created is None:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"failed to create page: {name}"))

    appended_count = await _append_tree_to_page(client, name, normalized_blocks)
    block_tree = await _get_page_blocks(client, name)

    try:
        page = PageEntity.model_validate(created)
    except Exception:
        page = await _get_page_or_error(client, name)

    page_view = page.model_dump(by_alias=False)
    page_view["name"] = page.display_name

    return json.dumps(
        {
            "page": page_view,
            "created": True,
            "blocks": [block.model_dump(by_alias=False) for block in block_tree],
            "block_count": _count_blocks(block_tree),
            "appended": appended_count,
        }
    )


@mcp.tool()
async def block_append(ctx: Context, page: str, blocks: list | str | dict) -> str:
    """Append blocks to an existing page. REQUIRES `page` — it has NO default and
    is never inferred from context (there is no notion of a "current page").

    You MUST pass the target page name explicitly in `page`. If you intend to write
    to a journal/day page, call `journal_append` (which derives the page from a
    date) or `journal_today` instead — do NOT call this tool hoping it will pick
    today's page.

    Pass `page` with natural casing and orthography (for example "Meeting Notes");
    Logseq resolves page names case-insensitively but preserves the supplied casing
    as the display name, so do not lowercase it.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("block_append: %s", page)

    try:
        normalized_blocks = _normalize_blocks(blocks)
    except Exception as exc:
        raise _normalize_error(exc)

    await _get_page_or_error(client, page)
    appended_count = await _append_tree_to_page(client, page, normalized_blocks)
    block_tree = await _get_page_blocks(client, page)

    return json.dumps(
        {
            "page": page,
            "appended": appended_count,
            "blocks": [block.model_dump(by_alias=False) for block in block_tree],
            "block_count": _count_blocks(block_tree),
        }
    )


@mcp.tool()
async def block_prepend(ctx: Context, page: str, blocks: list | str | dict) -> str:
    """Prepend blocks to the TOP of an existing page (above all existing content).

    REQUIRES `page` — it has NO default and is never inferred from context. If
    you intend to write to a journal/day page, call `journal_append` instead.

    Accepts the same block formats as `block_append` (flat strings or nested
    objects with content, properties, and children). Blocks appear at the top of
    the page in the order supplied, preserving the existing content below. Uses
    batch insertion for efficiency. Pass `page` with natural casing; do not
    lowercase it.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("block_prepend: %s", page)

    try:
        normalized_blocks = _normalize_blocks(blocks)
    except Exception as exc:
        raise _normalize_error(exc)

    await _get_page_or_error(client, page)
    prepended_count = await _append_tree_to_page(client, page, normalized_blocks, prepend=True)
    block_tree = await _get_page_blocks(client, page)

    return json.dumps(
        {
            "page": page,
            "prepended": prepended_count,
            "blocks": [block.model_dump(by_alias=False) for block in block_tree],
            "block_count": _count_blocks(block_tree),
        }
    )


@mcp.tool()
async def block_update(ctx: Context, uuid: str, content: str) -> str:
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("block_update: %s", uuid)

    await client._call("logseq.Editor.updateBlock", uuid, content)

    try:
        block = await _get_block_or_error(client, uuid)
        if block.content != content:
            logger.warning("block_update readback mismatch for %s", uuid)
        return json.dumps({"uuid": block.uuid, "content": block.content})
    except McpError:
        raise
    except Exception as exc:
        logger.warning("block_update readback failed for %s: %s", uuid, exc)
        return json.dumps({"uuid": uuid, "content": content})


@mcp.tool()
async def block_delete(ctx: Context, uuid: str) -> str:
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("block_delete: %s", uuid)

    block = await _get_block_or_error(client, uuid)
    await client._call("logseq.Editor.removeBlock", uuid)

    return json.dumps({"ok": True, "uuid": uuid})


@mcp.tool()
async def delete_page(ctx: Context, name: str) -> str:
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("delete_page: %s", name)

    await _verify_page_present(client, name)
    await client._call("logseq.Editor.deletePage", name)

    return json.dumps({"ok": True, "name": name})


@mcp.tool()
async def rename_page(ctx: Context, old_name: str, new_name: str) -> str:
    """Rename a page from old_name to new_name.

    Pass `new_name` with natural casing and orthography (for example "Renamed
    Page"). Logseq resolves page names case-insensitively and adopts the casing of
    `new_name` as the page's new display name, so do NOT lowercase or slugify it —
    that would make the rewritten page show up in lowercase in Logseq.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("rename_page: %s -> %s", old_name, new_name)

    _validate_rename_target(old_name, new_name)
    await _verify_rename_target_available(client, new_name)
    await client._call("logseq.Editor.renamePage", old_name, new_name)

    try:
        page = await _verify_page_present(client, new_name)
        if not _page_matches_name(page, new_name):
            logger.warning("rename_page readback name mismatch: expected %s", new_name)
    except McpError:
        raise
    except Exception as exc:
        logger.warning("rename_page readback failed for %s: %s", new_name, exc)

    return json.dumps({"ok": True, "old_name": old_name, "new_name": new_name})


@mcp.tool()
async def move_block(ctx: Context, uuid: str, target_uuid: str, position: str) -> str:
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    logger.info("move_block: %s %s %s", uuid, position, target_uuid)

    normalized_position = _validate_move_position(position)
    block = await _get_block_or_error(client, uuid)
    target = await _get_block_or_error(client, target_uuid)
    source_page_name = block.page.name if block.page and block.page.name else None
    destination_page_name = target.page.name if target.page and target.page.name else source_page_name
    if not destination_page_name:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"page block tree unavailable for move: {uuid}"))

    subtree_uuids = _collect_subtree_uuids(block)

    await client._call("logseq.Editor.moveBlock", uuid, target_uuid, _move_block_options(normalized_position))

    try:
        await _verify_block_move_readback(
            client,
            moved_uuid=uuid,
            target_uuid=target_uuid,
            position=normalized_position,
            destination_page_name=destination_page_name,
            source_page_name=None,
            subtree_uuids=subtree_uuids,
        )
    except McpError:
        raise
    except Exception as exc:
        logger.warning("move_block verification failed for %s: %s", uuid, exc)

    return json.dumps(
        {
            "ok": True,
            "uuid": uuid,
            "target_uuid": target_uuid,
            "position": normalized_position,
        }
    )


@mcp.tool()
async def journal_today(ctx: Context) -> str:
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    page_name = _resolve_journal_page_name()
    logger.info("journal_today: %s", page_name)

    page, created, block_tree = await _ensure_journal_page(client, page_name)
    return json.dumps(
        {
            "page": page.model_dump(by_alias=False),
            "created": created,
            "blocks": [block.model_dump(by_alias=False) for block in block_tree],
            "block_count": _count_blocks(block_tree),
        }
    )


_JOURNAL_RANGE_MAX_DAYS = 366


def _parse_journal_date(value: str, *, field: str) -> date:
    """Parse an ISO date string with an explicit McpError on invalid input."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"invalid {field}: {value}")) from exc


def _iter_inclusive_dates(start: date, end: date, *, max_days: int = _JOURNAL_RANGE_MAX_DAYS) -> list[date]:
    """Return a list of dates from start to end inclusive, bounded by max_days."""
    day_count = (end - start).days + 1
    if day_count > max_days:
        raise McpError(
            ErrorData(
                code=INTERNAL_ERROR,
                message=f"journal range exceeds maximum span of {max_days} days",
            )
        )
    return [start + timedelta(days=i) for i in range(day_count)]


@mcp.tool()
async def journal_append(ctx: Context, date: str, blocks: list | str | dict) -> str:
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    try:
        page_name = _resolve_journal_page_name(date)
        normalized_blocks = _normalize_blocks(blocks)
    except Exception as exc:
        raise _normalize_error(exc)

    logger.info("journal_append: %s", page_name)

    await _ensure_journal_page(client, page_name)
    appended_count = await _append_tree_to_page(client, page_name, normalized_blocks)
    block_tree = await _get_page_blocks(client, page_name)

    return json.dumps(
        {
            "page": page_name,
            "appended": appended_count,
            "blocks": [block.model_dump(by_alias=False) for block in block_tree],
            "block_count": _count_blocks(block_tree),
        }
    )


@mcp.tool()
async def journal_range(ctx: Context, start_date: str, end_date: str) -> str:
    app_ctx: AppContext = ctx.request_context.lifespan_context
    client = app_ctx.client

    start = _parse_journal_date(start_date, field="start_date")
    end = _parse_journal_date(end_date, field="end_date")

    if start > end:
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message="start_date must be on or before end_date")
        )

    dates = _iter_inclusive_dates(start, end)

    logger.info("journal_range: %s to %s (%d days)", start_date, end_date, len(dates))

    async def _fetch_journal_entry(day_str: str) -> dict | None:
        page = await _get_page_or_none(client, day_str)
        if page is None:
            return None
        if not page.journal:
            raise McpError(
                ErrorData(code=INTERNAL_ERROR, message=f"resolved page is not a journal page: {day_str}")
            )
        block_tree = await _get_page_blocks(client, day_str)
        return {
            "page": page.model_dump(by_alias=False),
            "blocks": [block.model_dump(by_alias=False) for block in block_tree],
            "block_count": _count_blocks(block_tree),
        }

    coros = [_fetch_journal_entry(day.isoformat()) for day in dates]
    results = await asyncio.gather(*coros, return_exceptions=True)

    entries = []
    for result in results:
        if isinstance(result, Exception):
            raise result
        if result is not None:
            entries.append(result)

    return json.dumps(
        {
            "start_date": start_date,
            "end_date": end_date,
            "days": len(dates),
            "entries": entries,
            "entry_count": len(entries),
        }
    )
