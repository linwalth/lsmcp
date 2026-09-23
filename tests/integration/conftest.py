from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import AsyncIterator, Awaitable, Callable
from uuid import uuid4

import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from logseq_mcp.client import LogseqClient

REQUIRED_GRAPH_NAME = "logseq-test-graph"
PARITY_PAGE_NAME = "Phase 04 Parity Fixture"
SANDBOX_PAGE_NAME = "Phase 04 Write Sandbox"
SANDBOX_BASELINE_BLOCKS = [
    "Sandbox baseline",
    "This page is reserved for Phase 4 live mutation tests.",
]
LIFECYCLE_PAGE_PREFIX = "Phase 05 Lifecycle"
MOVE_PAGE_PREFIX = "Phase 06 Move"
JOURNAL_PAGE_TITLE_FORMAT = "yyyy-MM-dd"
FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "graph"
MCP_CONFIG_ENV = "WORKSPACE_MCP_CONFIG"
MCP_CONFIG_DEFAULT = (Path.home() / "Workspace" / ".mcp.json").resolve()
GRAPHTHULHU_SERVER_NAME_ENV = "GRAPHTHULHU_MCP_SERVER"
GRAPHTHULHU_SERVER_NAME_DEFAULT = "graphthulhu"


@dataclass(frozen=True)
class IsolatedGraphEnv:
    api_url: str
    token: str
    graph_name: str
    parity_page: str
    sandbox_page: str


@dataclass
class StdioServerHandle:
    session: ClientSession
    stderr_buffer: object

    @property
    def stderr_text(self) -> str:
        self.stderr_buffer.flush()
        self.stderr_buffer.seek(0)
        return self.stderr_buffer.read()


@dataclass(frozen=True)
class ExternalMcpServerConfig:
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str | Path | None


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(
            f"{name} is required for live integration tests. "
            "Open the isolated Logseq graph first, then export the required environment."
        )
    return value


def _workspace_mcp_config_path() -> Path:
    configured = os.environ.get(MCP_CONFIG_ENV, "").strip()
    config_path = Path(configured).expanduser().resolve() if configured else MCP_CONFIG_DEFAULT
    if not config_path.is_file():
        pytest.fail(
            f"Workspace MCP config is required for graphthulhu parity tests. "
            f"Configured path does not exist: {config_path}"
        )
    return config_path


def _graphthulhu_server_name() -> str:
    server_name = os.environ.get(GRAPHTHULHU_SERVER_NAME_ENV, GRAPHTHULHU_SERVER_NAME_DEFAULT).strip()
    if not server_name:
        pytest.fail(f"{GRAPHTHULHU_SERVER_NAME_ENV} must be a non-empty MCP server name")
    return server_name


def _normalize_cli_args(args: list[str]) -> list[str]:
    normalized: list[str] = []
    expand_next_path = False

    for arg in args:
        if expand_next_path:
            normalized.append(str(Path(arg).expanduser()))
            expand_next_path = False
            continue

        normalized.append(str(Path(arg).expanduser()) if arg.startswith("~/") else arg)
        if arg in {"--project"}:
            expand_next_path = True

    return normalized


def _load_external_mcp_server_config(config_path: Path, server_name: str) -> ExternalMcpServerConfig:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        pytest.fail(f"Failed to parse workspace MCP config {config_path}: {exc}")

    if not isinstance(raw, dict):
        pytest.fail(f"Workspace MCP config must be a JSON object: {config_path}")

    servers = raw.get("mcpServers")
    if not isinstance(servers, dict):
        pytest.fail(f"Workspace MCP config must define an mcpServers object: {config_path}")

    config = servers.get(server_name)
    if not isinstance(config, dict):
        available = ", ".join(sorted(name for name in servers if isinstance(name, str)))
        pytest.fail(
            f"Workspace MCP config {config_path} does not define server '{server_name}'. "
            f"Available servers: {available or '(none)'}"
        )

    if not isinstance(config, dict):
        pytest.fail(f"Graphthulhu server config is invalid in {config_path}")

    command = config.get("command")
    args = config.get("args", [])
    env = config.get("env", {})
    cwd = config.get("cwd")

    if not isinstance(command, str) or not command.strip():
        pytest.fail(f"Graphthulhu config in {config_path} is missing a valid command")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        pytest.fail(f"Graphthulhu config in {config_path} must define args as a string list")
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        pytest.fail(f"Graphthulhu config in {config_path} must define env as a string map")
    if cwd is not None and not isinstance(cwd, str):
        pytest.fail(f"Graphthulhu config in {config_path} must define cwd as a string path")

    return ExternalMcpServerConfig(
        command=command,
        args=_normalize_cli_args(args),
        env=env,
        cwd=Path(cwd).expanduser() if cwd else None,
    )


async def _get_graph_name(client: LogseqClient) -> str:
    graph = await client._call("logseq.App.getCurrentGraph")
    if not isinstance(graph, dict):
        pytest.fail("logseq.App.getCurrentGraph returned an invalid payload")

    graph_name = graph.get("name")
    if not isinstance(graph_name, str) or not graph_name.strip():
        pytest.fail("Logseq did not report an active graph name")

    return graph_name


async def _assert_isolated_graph(client: LogseqClient, settings: IsolatedGraphEnv) -> str:
    graph_name = await _get_graph_name(client)
    if graph_name != settings.graph_name:
        pytest.fail(
            "Live integration tests are locked to the isolated graph. "
            f"Expected '{settings.graph_name}', got '{graph_name}'. "
            "Switch Logseq to the disposable test graph before running any integration selection."
        )
    return graph_name


async def _get_page_or_none(client: LogseqClient, page_name: str) -> dict | None:
    page = await client._call("logseq.Editor.getPage", page_name)
    if page is None:
        return None
    if not isinstance(page, dict):
        pytest.fail(f"logseq.Editor.getPage returned an invalid payload for '{page_name}'")
    return page


async def _ensure_page(client: LogseqClient, page_name: str) -> None:
    page = await _get_page_or_none(client, page_name)
    if page is not None:
        return

    created = await client._call(
        "logseq.Editor.createPage",
        page_name,
        {},
        {"createFirstBlock": False},
    )
    if not isinstance(created, dict):
        pytest.fail(f"Failed to create sandbox page '{page_name}'")


async def _get_page_blocks(client: LogseqClient, page_name: str) -> list[dict]:
    raw_blocks = await client._call("logseq.Editor.getPageBlocksTree", page_name)
    if not isinstance(raw_blocks, list):
        pytest.fail(f"logseq.Editor.getPageBlocksTree returned an invalid payload for '{page_name}'")
    return raw_blocks


def find_block_by_content(blocks: list[dict], content: str) -> dict | None:
    for block in blocks:
        if block.get("content") == content:
            return block

        children = block.get("children", [])
        if isinstance(children, list):
            match = find_block_by_content(children, content)
            if match is not None:
                return match

    return None


async def _reset_page_blocks(client: LogseqClient, page_name: str) -> None:
    for block in await _get_page_blocks(client, page_name):
        block_uuid = block.get("uuid")
        if not isinstance(block_uuid, str) or not block_uuid:
            pytest.fail(f"Top-level block on '{page_name}' is missing a uuid")
        await client._call("logseq.Editor.removeBlock", block_uuid)


async def _append_baseline_blocks(client: LogseqClient, page_name: str) -> None:
    for content in SANDBOX_BASELINE_BLOCKS:
        created = await client._call(
            "logseq.Editor.appendBlockInPage",
            page_name,
            content,
            {},
        )
        if not isinstance(created, dict) or not isinstance(created.get("uuid"), str):
            pytest.fail(f"Failed to seed sandbox block '{content}' on '{page_name}'")


async def _seed_fixture_graph(client: LogseqClient, settings: IsolatedGraphEnv) -> dict[str, str]:
    await _assert_isolated_graph(client, settings)

    if await _get_page_or_none(client, settings.parity_page) is None:
        pytest.fail(
            f"Required parity fixture page '{settings.parity_page}' is missing. "
            f"Copy the markdown fixtures from {FIXTURE_ROOT} into the isolated graph before running writes."
        )

    await _ensure_page(client, settings.sandbox_page)
    await _reset_page_blocks(client, settings.sandbox_page)
    await _append_baseline_blocks(client, settings.sandbox_page)

    return {"parity_page": settings.parity_page, "sandbox_page": settings.sandbox_page}


def make_lifecycle_page_name(label: str, namespace: str | None = None) -> str:
    normalized_label = "-".join(label.strip().split())
    if not normalized_label:
        pytest.fail("Lifecycle page label must be a non-empty string")

    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    suffix = uuid4().hex[:8]
    base_name = f"{LIFECYCLE_PAGE_PREFIX} {normalized_label} {timestamp}-{suffix}"
    if namespace:
        return f"{namespace}/{base_name}"
    return base_name


async def ensure_lifecycle_page(
    client: LogseqClient,
    page_name: str,
    blocks: list[str] | None = None,
) -> dict:
    page = await _get_page_or_none(client, page_name)
    if page is None:
        created = await client._call(
            "logseq.Editor.createPage",
            page_name,
            {},
            {"createFirstBlock": False},
        )
        if not isinstance(created, dict):
            pytest.fail(f"Failed to create lifecycle page '{page_name}'")

    if blocks:
        if page is not None:
            await _reset_page_blocks(client, page_name)
        for content in blocks:
            created = await client._call(
                "logseq.Editor.appendBlockInPage",
                page_name,
                content,
                {},
            )
            if not isinstance(created, dict) or not isinstance(created.get("uuid"), str):
                pytest.fail(f"Failed to seed lifecycle block '{content}' on '{page_name}'")

    page = await _get_page_or_none(client, page_name)
    if page is None:
        pytest.fail(f"Lifecycle page did not resolve after setup: '{page_name}'")
    return page


async def cleanup_lifecycle_page(client: LogseqClient, page_name: str) -> None:
    page = await _get_page_or_none(client, page_name)
    if page is None:
        return

    await client._call("logseq.Editor.deletePage", page_name)
    deleted = await _get_page_or_none(client, page_name)
    if deleted is not None:
        pytest.fail(f"Lifecycle page still resolves after cleanup: '{page_name}'")


def make_journal_page_name(days_from_today: int = 0) -> str:
    if not isinstance(days_from_today, int):
        pytest.fail("Journal page day offset must be an integer")
    if JOURNAL_PAGE_TITLE_FORMAT != "yyyy-MM-dd":
        pytest.fail(f"Unsupported journal page title format for tests: {JOURNAL_PAGE_TITLE_FORMAT}")

    return (date.today() + timedelta(days=days_from_today)).isoformat()


async def cleanup_journal_page(client: LogseqClient, page_name: str) -> None:
    await cleanup_lifecycle_page(client, page_name)


def make_move_page_name(label: str) -> str:
    normalized_label = "-".join(label.strip().split())
    if not normalized_label:
        pytest.fail("Move page label must be a non-empty string")

    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    suffix = uuid4().hex[:8]
    return f"{MOVE_PAGE_PREFIX} {normalized_label} {timestamp}-{suffix}"


async def _append_block(client: LogseqClient, page_name: str, content: str) -> str:
    created = await client._call("logseq.Editor.appendBlockInPage", page_name, content, {})
    if not isinstance(created, dict) or not isinstance(created.get("uuid"), str) or not created["uuid"]:
        pytest.fail(f"Failed to append block '{content}' on '{page_name}'")
    return created["uuid"]


async def _insert_child_block(client: LogseqClient, parent_uuid: str, content: str) -> str:
    created = await client._call("logseq.Editor.insertBlock", parent_uuid, content, {"sibling": False})
    if not isinstance(created, dict) or not isinstance(created.get("uuid"), str) or not created["uuid"]:
        pytest.fail(f"Failed to insert child block '{content}' under '{parent_uuid}'")
    return created["uuid"]


async def ensure_move_page(client: LogseqClient, page_name: str) -> dict[str, str]:
    page = await ensure_lifecycle_page(client, page_name)
    await _reset_page_blocks(client, page_name)

    anchor_a_uuid = await _append_block(client, page_name, "Move anchor A")
    anchor_a_child_uuid = await _insert_child_block(client, anchor_a_uuid, "Move anchor A child")
    source_uuid = await _append_block(client, page_name, "Move source")
    source_child_uuid = await _insert_child_block(client, source_uuid, "Move source child")
    source_grandchild_uuid = await _insert_child_block(client, source_child_uuid, "Move source grandchild")
    anchor_b_uuid = await _append_block(client, page_name, "Move anchor B")

    return {
        "page_name": page_name,
        "page_uuid": str(page["uuid"]),
        "anchor_a_uuid": anchor_a_uuid,
        "anchor_a_child_uuid": anchor_a_child_uuid,
        "source_uuid": source_uuid,
        "source_child_uuid": source_child_uuid,
        "source_grandchild_uuid": source_grandchild_uuid,
        "anchor_b_uuid": anchor_b_uuid,
    }


async def _ensure_cross_page_move_fixture(
    client: LogseqClient,
    source_page_name: str,
    destination_page_name: str,
) -> dict[str, str]:
    source_page = await ensure_lifecycle_page(client, source_page_name)
    destination_page = await ensure_lifecycle_page(client, destination_page_name)
    await _reset_page_blocks(client, source_page_name)
    await _reset_page_blocks(client, destination_page_name)

    source_anchor_uuid = await _append_block(client, source_page_name, "Cross-page source anchor")
    source_uuid = await _append_block(client, source_page_name, "Cross-page move source")
    source_child_uuid = await _insert_child_block(client, source_uuid, "Cross-page move child")
    source_grandchild_uuid = await _insert_child_block(client, source_child_uuid, "Cross-page move grandchild")
    destination_anchor_uuid = await _append_block(client, destination_page_name, "Cross-page target anchor")

    return {
        "source_page_name": source_page_name,
        "source_page_uuid": str(source_page["uuid"]),
        "destination_page_name": destination_page_name,
        "destination_page_uuid": str(destination_page["uuid"]),
        "source_anchor_uuid": source_anchor_uuid,
        "source_uuid": source_uuid,
        "source_child_uuid": source_child_uuid,
        "source_grandchild_uuid": source_grandchild_uuid,
        "destination_anchor_uuid": destination_anchor_uuid,
    }


def _build_stdio_env(settings: IsolatedGraphEnv) -> dict[str, str]:
    env = os.environ.copy()
    env["LOGSEQ_API_URL"] = settings.api_url
    env["LOGSEQ_API_TOKEN"] = settings.token
    env["LOGSEQ_TEST_GRAPH_NAME"] = settings.graph_name
    env["LOGSEQ_TEST_PARITY_PAGE"] = settings.parity_page
    env["LOGSEQ_TEST_SANDBOX_PAGE"] = settings.sandbox_page
    return env


def _build_external_stdio_env(
    settings: IsolatedGraphEnv,
    server_env: dict[str, str],
) -> dict[str, str]:
    env = _build_stdio_env(settings)
    env.update(server_env)
    env["LOGSEQ_API_URL"] = settings.api_url
    env["LOGSEQ_API_TOKEN"] = settings.token
    env["LOGSEQ_TEST_GRAPH_NAME"] = settings.graph_name
    env["LOGSEQ_TEST_PARITY_PAGE"] = settings.parity_page
    env["LOGSEQ_TEST_SANDBOX_PAGE"] = settings.sandbox_page
    return env


@pytest.fixture
def isolated_graph_env() -> IsolatedGraphEnv:
    return IsolatedGraphEnv(
        api_url=os.environ.get("LOGSEQ_API_URL", "http://127.0.0.1:12315"),
        token=_require_env("LOGSEQ_API_TOKEN"),
        graph_name=os.environ.get("LOGSEQ_TEST_GRAPH_NAME", REQUIRED_GRAPH_NAME),
        parity_page=os.environ.get("LOGSEQ_TEST_PARITY_PAGE", PARITY_PAGE_NAME),
        sandbox_page=os.environ.get("LOGSEQ_TEST_SANDBOX_PAGE", SANDBOX_PAGE_NAME),
    )


def _launch_stdio_server(server: StdioServerParameters) -> AsyncIterator[StdioServerHandle]:
    @asynccontextmanager
    async def _runner() -> AsyncIterator[StdioServerHandle]:
        stderr_buffer = TemporaryFile(mode="w+", encoding="utf-8")

        try:
            async with stdio_client(server, errlog=stderr_buffer) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield StdioServerHandle(session=session, stderr_buffer=stderr_buffer)
        finally:
            stderr_buffer.close()

    return _runner()


def launch_stdio_server(
    isolated_graph_env: IsolatedGraphEnv,
) -> AsyncIterator[StdioServerHandle]:
    project_root = Path(__file__).resolve().parents[2]
    server = StdioServerParameters(
        command="uv",
        args=["run", "--project", str(project_root), "lsmcp"],
        env=_build_stdio_env(isolated_graph_env),
        cwd=project_root,
    )
    return _launch_stdio_server(server)


def launch_external_stdio_server(
    isolated_graph_env: IsolatedGraphEnv,
    external_config: ExternalMcpServerConfig,
) -> AsyncIterator[StdioServerHandle]:
    server = StdioServerParameters(
        command=external_config.command,
        args=external_config.args,
        env=_build_external_stdio_env(isolated_graph_env, external_config.env),
        cwd=external_config.cwd,
    )
    return _launch_stdio_server(server)


@pytest_asyncio.fixture
async def live_client(monkeypatch: pytest.MonkeyPatch, isolated_graph_env: IsolatedGraphEnv) -> AsyncIterator[LogseqClient]:
    monkeypatch.setenv("LOGSEQ_API_URL", isolated_graph_env.api_url)
    monkeypatch.setenv("LOGSEQ_API_TOKEN", isolated_graph_env.token)

    async with LogseqClient() as client:
        yield client


@pytest.fixture
def assert_isolated_graph(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[LogseqClient], Awaitable[str]]:
    async def _runner(client: LogseqClient) -> str:
        return await _assert_isolated_graph(client, isolated_graph_env)

    return _runner


@pytest.fixture
def mcp_session(isolated_graph_env: IsolatedGraphEnv) -> AsyncIterator[StdioServerHandle]:
    return launch_stdio_server(isolated_graph_env)


@pytest.fixture
def graphthulhu_mcp_config() -> ExternalMcpServerConfig:
    return _load_external_mcp_server_config(_workspace_mcp_config_path(), _graphthulhu_server_name())


@pytest.fixture
def graphthulhu_mcp_session(
    isolated_graph_env: IsolatedGraphEnv,
    graphthulhu_mcp_config: ExternalMcpServerConfig,
) -> AsyncIterator[StdioServerHandle]:
    return launch_external_stdio_server(isolated_graph_env, graphthulhu_mcp_config)


@pytest.fixture
def seed_fixture_graph(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[LogseqClient], Awaitable[dict[str, str]]]:
    async def _runner(client: LogseqClient) -> dict[str, str]:
        return await _seed_fixture_graph(client, isolated_graph_env)

    return _runner


@pytest.fixture
def lifecycle_page_factory(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[str], str]:
    def _runner(label: str, namespace: str | None = None) -> str:
        page_name = make_lifecycle_page_name(label, namespace)
        if page_name in {isolated_graph_env.parity_page, isolated_graph_env.sandbox_page}:
            pytest.fail(f"Lifecycle page factory produced a reserved fixture page: {page_name}")
        return page_name

    return _runner


@pytest.fixture
def move_page_factory(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[str], str]:
    def _runner(label: str) -> str:
        page_name = make_move_page_name(label)
        if page_name in {isolated_graph_env.parity_page, isolated_graph_env.sandbox_page}:
            pytest.fail(f"Move page factory produced a reserved fixture page: {page_name}")
        return page_name

    return _runner


@pytest.fixture
def journal_page_factory() -> Callable[[int], str]:
    def _runner(days_from_today: int = 30) -> str:
        return make_journal_page_name(days_from_today)

    return _runner


@pytest.fixture
def journal_append_date_factory() -> Callable[[int], str]:
    def _runner(days_from_today: int = 30) -> str:
        return make_journal_page_name(days_from_today)

    return _runner


@pytest.fixture
def ensure_lifecycle_page_fixture(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[LogseqClient, str, list[str] | None], Awaitable[dict]]:
    async def _runner(client: LogseqClient, page_name: str, blocks: list[str] | None = None) -> dict:
        await _assert_isolated_graph(client, isolated_graph_env)
        return await ensure_lifecycle_page(client, page_name, blocks)

    return _runner


@pytest.fixture
def cleanup_lifecycle_page_fixture(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[LogseqClient, str], Awaitable[None]]:
    async def _runner(client: LogseqClient, page_name: str) -> None:
        await _assert_isolated_graph(client, isolated_graph_env)
        await cleanup_lifecycle_page(client, page_name)

    return _runner


@pytest.fixture
def cleanup_journal_page_fixture(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[LogseqClient, str], Awaitable[None]]:
    async def _runner(client: LogseqClient, page_name: str) -> None:
        await _assert_isolated_graph(client, isolated_graph_env)
        await cleanup_journal_page(client, page_name)

    return _runner


@pytest.fixture
def ensure_move_page_fixture(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[LogseqClient, str], Awaitable[dict[str, str]]]:
    async def _runner(client: LogseqClient, page_name: str) -> dict[str, str]:
        await _assert_isolated_graph(client, isolated_graph_env)
        return await ensure_move_page(client, page_name)

    return _runner


@pytest.fixture
def ensure_cross_page_move_fixture(
    isolated_graph_env: IsolatedGraphEnv,
) -> Callable[[LogseqClient, str, str], Awaitable[dict[str, str]]]:
    async def _runner(client: LogseqClient, source_page_name: str, destination_page_name: str) -> dict[str, str]:
        await _assert_isolated_graph(client, isolated_graph_env)
        return await _ensure_cross_page_move_fixture(client, source_page_name, destination_page_name)

    return _runner


def assert_protocol_clean_stdout(stderr_text: str) -> None:
    protocol_markers = ('"jsonrpc"', '"method"', '"result"', '"params"', '"id"')
    if any(marker in stderr_text for marker in protocol_markers):
        pytest.fail(f"stderr contains MCP protocol traffic: {stderr_text!r}")
