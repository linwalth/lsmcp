import asyncio
import json
import logging
import os
import time

import httpx
from mcp import McpError
from mcp.types import ErrorData, INTERNAL_ERROR

logger = logging.getLogger(__name__)

# Read-only RPCs whose results are safe to memoize for a short TTL.
_CACHEABLE_METHODS = frozenset({
    "logseq.App.getCurrentGraph",
    "logseq.Editor.getAllPages",
    "logseq.Editor.getPage",
    "logseq.Editor.getPageBlocksTree",
    "logseq.Editor.getBlock",
    "logseq.Editor.getPagesFromNamespace",
    "logseq.Editor.getPagesTreeFromNamespace",
    "logseq.Editor.getPageLinkedReferences",
})

# Substrings identifying mutating RPCs — seeing one flushes the whole cache so
# subsequent reads observe the change rather than a stale snapshot.
_WRITE_MARKERS = (
    "createPage", "updateBlock", "removeBlock", "deletePage", "renamePage",
    "moveBlock", "insertBatchBlock", "appendBlockInPage", "prependBlockInPage",
    "insertBlock", "editBlock", "exitEditingMode",
)


def _is_write(method: str) -> bool:
    return any(marker in method for marker in _WRITE_MARKERS)


class LogseqClient:
    def __init__(self) -> None:
        self._url = os.environ.get("LOGSEQ_API_URL", "http://127.0.0.1:12315")
        self._token = os.environ.get("LOGSEQ_API_TOKEN")
        if not self._token:
            raise RuntimeError(
                "LOGSEQ_API_TOKEN environment variable is required. "
                "Set it in your MCP client config or shell environment."
            )
        self._sem = asyncio.Semaphore(4)
        self._http: httpx.AsyncClient | None = None
        ttl = float(os.environ.get("LOGSEQ_CACHE_TTL_SECONDS", "60"))
        self._cache_ttl = ttl if ttl > 0 else 0.0
        self._cache: dict[tuple, tuple[float, object]] = {}

    async def __aenter__(self) -> "LogseqClient":
        timeout = float(os.environ.get("LOGSEQ_API_TIMEOUT", "30"))
        limits = httpx.Limits(
            max_keepalive_connections=20,
            max_connections=50,
            keepalive_expiry=30.0,
        )
        self._http = httpx.AsyncClient(timeout=timeout, limits=limits)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
        self._cache.clear()

    def invalidate_cache(self) -> None:
        self._cache.clear()

    def _cache_key(self, method: str, args: tuple) -> tuple:
        return (method, json.dumps(args, default=str, sort_keys=True))

    async def _call(self, method: str, *args: object) -> object:
        assert self._http is not None, "LogseqClient must be used as async context manager"

        if _is_write(method):
            self.invalidate_cache()
        elif self._cache_ttl and method in _CACHEABLE_METHODS:
            key = self._cache_key(method, args)
            entry = self._cache.get(key)
            if entry is not None and entry[0] > time.monotonic():
                logger.debug("cache hit %s", method)
                return entry[1]

        payload = {"method": method, "args": list(args)}
        headers = {"Authorization": f"Bearer {self._token}"}
        backoff = 0.1
        last_err: Exception | None = None

        async with self._sem:
            for attempt in range(4):  # 1 initial + 3 retries
                if attempt > 0:
                    logger.debug("Retry %d for %s (backoff %.1fs)", attempt, method, backoff)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                try:
                    resp = await self._http.post(
                        f"{self._url}/api",
                        json=payload,
                        headers=headers,
                    )
                except httpx.TransportError as e:
                    last_err = e
                    logger.debug("Transport error on %s: %s", method, e)
                    continue
                if resp.status_code >= 500:
                    last_err = Exception(
                        f"Logseq API error {resp.status_code}: {resp.text}"
                    )
                    continue
                if resp.status_code != 200:
                    raise McpError(
                        ErrorData(
                            code=INTERNAL_ERROR,
                            message=f"Logseq API error {resp.status_code}: {resp.text}",
                        )
                    )
                logger.debug("OK %s -> %d", method, resp.status_code)
                result = resp.json()

                if self._cache_ttl and method in _CACHEABLE_METHODS:
                    key = self._cache_key(method, args)
                    self._cache[key] = (time.monotonic() + self._cache_ttl, result)

                return result

        # Exhausted retries
        if isinstance(last_err, httpx.ConnectError):
            raise McpError(
                ErrorData(
                    code=INTERNAL_ERROR,
                    message=f"Logseq is not running or unreachable at {self._url}",
                )
            )
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message=str(last_err))
        )
