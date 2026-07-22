"""MCP 专用后台 asyncio 事件循环（仿 Hermes _mcp_loop）。

Phase 1 实现要点：
- daemon 线程持有单一 event loop
- run_coroutine_threadsafe 供同步 ToolCard.handler 调度异步 call_tool
- 应用退出时 graceful shutdown
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_mcp_loop: asyncio.AbstractEventLoop | None = None
_mcp_thread: threading.Thread | None = None
_lock = threading.Lock()


def ensure_mcp_loop() -> asyncio.AbstractEventLoop:
    """启动（或返回已有）MCP 后台事件循环。"""
    global _mcp_loop, _mcp_thread
    with _lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return _mcp_loop

        ready = threading.Event()

        def _run() -> None:
            global _mcp_loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _mcp_loop = loop
            ready.set()
            loop.run_forever()

        _mcp_thread = threading.Thread(target=_run, name="mcp-event-loop", daemon=True)
        _mcp_thread.start()
        ready.wait(timeout=5.0)
        if _mcp_loop is None:
            raise RuntimeError("MCP 后台事件循环启动失败")
        return _mcp_loop


def run_on_mcp_loop(coro: Coroutine[Any, Any, T], *, timeout: float = 30.0) -> T:
    """在 MCP 循环上执行协程并阻塞等待结果（供同步 handler 调用）。"""
    loop = ensure_mcp_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def stop_mcp_loop() -> None:
    """停止后台循环（应用退出时调用）。"""
    global _mcp_loop, _mcp_thread
    with _lock:
        if _mcp_loop is None:
            return
        loop = _mcp_loop
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if _mcp_thread is not None:
            _mcp_thread.join(timeout=5.0)
        _mcp_loop = None
        _mcp_thread = None
