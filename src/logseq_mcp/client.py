import asyncio
import logging
import os

import httpx
from mcp import McpError
from mcp.types import ErrorData, INTERNAL_ERROR

logger = logging.getLogger(__name__)


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

    async def __aenter__(self) -> "LogseqClient":
        self._http = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _call(self, method: str, *args: object) -> object:
        assert self._http is not None, "LogseqClient must be used as async context manager"
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
                return resp.json()

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
