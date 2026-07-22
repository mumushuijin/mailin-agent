"""单 MCP Server 长连接 Task。

已实现：HTTP / Streamable HTTP / SSE
待实现：stdio 子进程（见 LOCAL_MCP.md）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from app.tools.mcp.types import McpServerConfig, McpTransport

logger = logging.getLogger(__name__)

_MAX_RECONNECT_ATTEMPTS = 3
_RECONNECT_BASE_DELAY = 2.0


class MCPServerTask:
    """管理单个 MCP Server 的连接生命周期。"""

    __slots__ = (
        "name",
        "config",
        "session",
        "tools",
        "_task",
        "_ready",
        "_shutdown",
        "_rpc_lock",
        "initialize_result",
        "error",
        "_max_reconnect_attempts",
    )

    def __init__(
        self,
        name: str,
        config: McpServerConfig,
        *,
        max_reconnect_attempts: int | None = None,
    ):
        self.name = name
        self.config = config
        self.session: Any = None
        self.tools: list[Any] = []
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._rpc_lock = asyncio.Lock()
        self.initialize_result: Any = None
        self.error: str | None = None
        self._max_reconnect_attempts = (
            max_reconnect_attempts if max_reconnect_attempts is not None else _MAX_RECONNECT_ATTEMPTS
        )

    async def start(self) -> None:
        """在 MCP 事件循环上启动连接 Task。"""
        if self._task is not None and not self._task.done():
            return
        self._shutdown.clear()
        self._ready.clear()
        self._task = asyncio.create_task(self._run(), name=f"mcp-server-{self.name}")

    async def _run(self) -> None:
        attempt = 0
        max_attempts = max(1, self._max_reconnect_attempts)
        while not self._shutdown.is_set():
            try:
                if self.config.transport == McpTransport.HTTP:
                    await self._run_http_once()
                elif self.config.transport == McpTransport.SSE:
                    await self._run_sse_once()
                else:
                    self.error = "stdio 传输尚未实现，请参阅 app/tools/mcp/LOCAL_MCP.md"
                    logger.warning("MCP server '%s': %s", self.name, self.error)
                    return
                if self._shutdown.is_set():
                    break
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc)
                self.session = None
                self.tools = []
                self._ready.clear()
                attempt += 1
                if attempt >= max_attempts or self._shutdown.is_set():
                    if max_attempts > 1:
                        logger.warning(
                            "MCP server '%s' 连接失败（已重试 %d 次）: %s",
                            self.name,
                            attempt,
                            exc,
                        )
                    return
                delay = _RECONNECT_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    "MCP server '%s' 将在 %.1fs 后重连（第 %d 次）",
                    self.name,
                    delay,
                    attempt,
                )
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    continue

    async def _open_session(self, read, write) -> None:
        async with ClientSession(read, write) as session:
            self.session = session
            self.initialize_result = await session.initialize()

            if not self._advertises_tools():
                self.tools = []
            else:
                async with self._rpc_lock:
                    tools_result = await session.list_tools()
                self.tools = list(
                    tools_result.tools if hasattr(tools_result, "tools") else []
                )

            self.error = None
            self._ready.set()
            transport_label = self.config.transport.value.upper()
            logger.info(
                "MCP server '%s' 已连接（%s），发现 %d 个工具",
                self.name,
                transport_label,
                len(self.tools),
            )

            await self._shutdown.wait()

            self.session = None
            self.tools = []
            self._ready.clear()

    async def _run_http_once(self) -> None:
        url = (self.config.url or "").strip()
        if not url:
            raise ValueError(f"MCP server '{self.name}' 缺少 url")

        timeout = httpx.Timeout(
            self.config.connect_timeout,
            read=max(self.config.timeout, self.config.connect_timeout),
        )
        httpx_client = create_mcp_http_client(
            headers=self.config.headers or None,
            timeout=timeout,
        )

        async with httpx_client:
            async with streamable_http_client(url, http_client=httpx_client) as (read, write, _):
                await self._open_session(read, write)

    async def _run_sse_once(self) -> None:
        url = (self.config.url or "").strip()
        if not url:
            raise ValueError(f"MCP server '{self.name}' 缺少 url")

        connect_timeout = float(self.config.connect_timeout)
        read_timeout = max(self.config.timeout, connect_timeout)
        async with sse_client(
            url,
            headers=self.config.headers or None,
            timeout=connect_timeout,
            sse_read_timeout=read_timeout,
        ) as (read, write):
            await self._open_session(read, write)

    def _advertises_tools(self) -> bool:
        init_result = self.initialize_result
        caps = getattr(init_result, "capabilities", None) if init_result is not None else None
        if caps is None:
            return True
        return getattr(caps, "tools", None) is not None

    async def shutdown(self) -> None:
        self._shutdown.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("MCP server '%s' shutdown: %s", self.name, exc)
            self._task = None
        self.session = None
        self.tools = []
        self._ready.clear()

    async def wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
