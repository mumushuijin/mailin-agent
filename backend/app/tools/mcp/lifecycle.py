from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time

from app.tools.mcp.bridge import get_mcp_cards, set_mcp_cards
from app.tools.mcp.config import load_mcp_server_configs
from app.tools.mcp.discovery import build_cards_for_server
from app.tools.mcp.loop import ensure_mcp_loop, run_on_mcp_loop, stop_mcp_loop
from app.tools.mcp.server_task import MCPServerTask
from app.tools.mcp.types import McpServerStatus

logger = logging.getLogger(__name__)

_MCP_SDK_AVAILABLE = False
try:
    import mcp  # noqa: F401

    _MCP_SDK_AVAILABLE = True
except ImportError:
    pass

_servers: dict[str, MCPServerTask] = {}
_server_errors: dict[str, str] = {}
_connect_failures: dict[str, int] = {}
_lock = threading.Lock()
_discover_lock = threading.Lock()
_bg_thread: threading.Thread | None = None
_discover_running = False

_MAX_CONNECT_ATTEMPTS = 3
_DISCOVERY_READY_CAP_SEC = 10.0
MCP_STATUS_CACHE_TTL_SECONDS = 5.0
_mcp_status_cache: list[McpServerStatus] | None = None
_mcp_status_cache_at = 0.0


def _invalidate_mcp_status_cache_locked() -> None:
    global _mcp_status_cache, _mcp_status_cache_at
    _mcp_status_cache = None
    _mcp_status_cache_at = 0.0


def clear_mcp_status_cache() -> None:
    with _lock:
        _invalidate_mcp_status_cache_locked()


def _clear_tools_cache_quietly() -> None:
    clear_mcp_status_cache()
    try:
        from app.tools.registry import clear_tools_cache

        clear_tools_cache()
    except Exception:
        pass


def get_mcp_server(name: str) -> MCPServerTask | None:
    with _lock:
        return _servers.get(name)


def _server_is_live(server: MCPServerTask | None) -> bool:
    return (
        server is not None
        and server._ready.is_set()
        and server.session is not None
    )


def server_connect_gave_up(name: str) -> bool:
    """该 Server 是否已达最大连接重试次数（不再阻塞主线程重连）。"""
    with _lock:
        return _connect_failures.get(name, 0) >= _MAX_CONNECT_ATTEMPTS


def get_server_connect_error(name: str) -> str | None:
    with _lock:
        return _server_errors.get(name)


def _record_connect_failure(name: str, err: str) -> int:
    with _lock:
        count = _connect_failures.get(name, 0) + 1
        _connect_failures[name] = count
        if count >= _MAX_CONNECT_ATTEMPTS:
            _server_errors[name] = f"{err}（已重试 {count} 次，暂停自动连接）"
        else:
            _server_errors[name] = err
        _invalidate_mcp_status_cache_locked()
        return count


def _record_connect_success(name: str) -> None:
    with _lock:
        _connect_failures.pop(name, None)
        _server_errors.pop(name, None)
        _invalidate_mcp_status_cache_locked()


def _reset_connect_failures() -> None:
    with _lock:
        _connect_failures.clear()
        _invalidate_mcp_status_cache_locked()


def _servers_pending_connect(configs: dict) -> list[str]:
    """仍值得尝试连接的 Server（未就绪且未放弃）。"""
    pending: list[str] = []
    with _lock:
        for name in configs:
            if _server_is_live(_servers.get(name)):
                continue
            if _connect_failures.get(name, 0) >= _MAX_CONNECT_ATTEMPTS:
                continue
            pending.append(name)
    return pending


def _in_running_async_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _schedule_mcp_discover_background() -> None:
    """在后台线程执行 MCP 发现，避免阻塞 ASGI 事件循环。"""
    global _bg_thread
    if not _servers_pending_connect(load_mcp_server_configs() or {}):
        return
    with _discover_lock:
        if _discover_running:
            return
        if _bg_thread is not None and _bg_thread.is_alive():
            return

        def _run() -> None:
            try:
                discover_mcp_servers()
            except Exception as exc:
                logger.warning("MCP 后台发现失败: %s", exc)

        _bg_thread = threading.Thread(target=_run, name="mcp-discover", daemon=True)
        _bg_thread.start()


def ensure_mcp_connected(*, blocking: bool | None = None) -> bool:
    """确保已配置的 MCP Server 已连接。

    langgraph dev 在 async 上下文中调用 get_graph 时不能同步阻塞；
    此时仅调度后台发现线程，连接在工具执行或 FastAPI lifespan 中完成。
    """
    if blocking is None:
        blocking = not _in_running_async_loop()

    if not _MCP_SDK_AVAILABLE:
        return False
    configs = load_mcp_server_configs()
    if not configs:
        return False

    if not _servers_pending_connect(configs):
        with _lock:
            return any(_server_is_live(_servers.get(name)) for name in configs)

    if not blocking:
        _schedule_mcp_discover_background()
        return False

    discover_mcp_servers()
    with _lock:
        return any(_server_is_live(_servers.get(name)) for name in configs)


def discover_mcp_servers() -> list[str]:
    """连接配置中的 MCP Server 并注册 ToolCard。"""
    global _discover_running

    if not _MCP_SDK_AVAILABLE:
        logger.warning("未安装 mcp 包，跳过 MCP 发现。请执行: uv pip install -e '.[mcp]'")
        set_mcp_cards([])
        _clear_tools_cache_quietly()
        return []

    configs = load_mcp_server_configs()
    if not configs:
        set_mcp_cards([])
        _clear_tools_cache_quietly()
        return []

    with _discover_lock:
        if _discover_running:
            logger.debug("MCP discover 已在运行，跳过重复调度")
            return [c.name for c in get_mcp_cards()]
        _discover_running = True

    try:
        return _discover_mcp_servers_locked(configs)
    finally:
        with _discover_lock:
            _discover_running = False


def _discover_mcp_servers_locked(configs: dict) -> list[str]:
    ensure_mcp_loop()

    async def _discover_all() -> list[str]:
        all_cards: list = []
        registered_names: list[str] = []
        connect_jobs: list[tuple[str, Any]] = []

        async def _connect_one(name: str, cfg) -> list:
            server = MCPServerTask(name, cfg, max_reconnect_attempts=1)
            await server.start()
            ready_timeout = min(cfg.connect_timeout, _DISCOVERY_READY_CAP_SEC)
            ready = await server.wait_ready(ready_timeout)
            if not ready:
                err = server.error or f"连接超时（{ready_timeout}s）"
                count = _record_connect_failure(name, err)
                await server.shutdown()
                logger.warning(
                    "MCP server '%s' 未就绪（第 %d/%d 次）: %s",
                    name,
                    count,
                    _MAX_CONNECT_ATTEMPTS,
                    err,
                )
                return []

            with _lock:
                _servers[name] = server
            _record_connect_success(name)

            cards = build_cards_for_server(server, cfg, get_server=get_mcp_server)
            logger.info(
                "MCP server '%s': 注册 %d 个工具 → %s",
                name,
                len(cards),
                ", ".join(c.name for c in cards) or "(无)",
            )
            return cards

        for name, cfg in configs.items():
            if server_connect_gave_up(name):
                continue
            with _lock:
                existing = _servers.get(name)
            if _server_is_live(existing):
                cards = build_cards_for_server(existing, cfg, get_server=get_mcp_server)
                all_cards.extend(cards)
                registered_names.extend(c.name for c in cards)
                continue

            if existing is not None:
                try:
                    await existing.shutdown()
                except Exception as exc:
                    logger.debug("MCP server '%s' 清理旧连接: %s", name, exc)
                with _lock:
                    _servers.pop(name, None)

            connect_jobs.append((name, cfg))

        if connect_jobs:
            results = await asyncio.gather(
                *(_connect_one(name, cfg) for name, cfg in connect_jobs),
                return_exceptions=True,
            )
            for (name, _), result in zip(connect_jobs, results):
                if isinstance(result, Exception):
                    count = _record_connect_failure(name, str(result))
                    logger.warning(
                        "MCP server '%s' 连接异常（第 %d/%d 次）: %s",
                        name,
                        count,
                        _MAX_CONNECT_ATTEMPTS,
                        result,
                    )
                elif result:
                    all_cards.extend(result)
                    registered_names.extend(c.name for c in result)

        set_mcp_cards(all_cards)
        _clear_tools_cache_quietly()
        return registered_names

    pending = len(_servers_pending_connect(configs))
    timeout = min(90.0, max(15.0, pending * (_DISCOVERY_READY_CAP_SEC + 2)))
    try:
        return run_on_mcp_loop(_discover_all(), timeout=timeout)
    except Exception as exc:
        logger.warning("MCP 发现失败: %s", exc)
        return []


def reload_mcp_servers() -> list[str]:
    """关闭并重连所有 MCP Server。"""
    _reset_connect_failures()
    shutdown_mcp_servers()
    return discover_mcp_servers()


def reset_mcp_server(name: str) -> None:
    """单 Server 断线重连（工具调用遇传输错误时快速恢复，避免 rpc_lock 长时间占用）。"""
    configs = load_mcp_server_configs()
    if name not in configs:
        return

    with _lock:
        server = _servers.pop(name, None)
        _connect_failures.pop(name, None)
        _server_errors.pop(name, None)
    if server is not None:

        async def _shutdown() -> None:
            await server.shutdown()

        try:
            run_on_mcp_loop(_shutdown(), timeout=15)
        except Exception as exc:
            logger.debug("MCP server '%s' 重置时 shutdown 异常: %s", name, exc)

    discover_mcp_servers()


def shutdown_mcp_servers() -> None:
    """关闭所有 MCP 连接与子进程。"""
    with _lock:
        servers = list(_servers.values())
        _servers.clear()

    async def _shutdown_all() -> None:
        await asyncio.gather(
            *(srv.shutdown() for srv in servers),
            return_exceptions=True,
        )

    if servers:
        try:
            run_on_mcp_loop(_shutdown_all(), timeout=30)
        except Exception as exc:
            logger.warning("MCP shutdown 异常: %s", exc)

    set_mcp_cards([])
    stop_mcp_loop()
    _clear_tools_cache_quietly()


def get_mcp_status() -> list[McpServerStatus]:
    """返回各 Server 连接状态（非阻塞；已放弃的 Server 不再重试）。"""
    global _mcp_status_cache, _mcp_status_cache_at
    now = time.monotonic()
    with _lock:
        if _mcp_status_cache is not None and now - _mcp_status_cache_at <= MCP_STATUS_CACHE_TTL_SECONDS:
            return copy.deepcopy(_mcp_status_cache)

    ensure_mcp_connected(blocking=False)
    configs = load_mcp_server_configs()
    cards = get_mcp_cards()
    by_package: dict[str, list[str]] = {}
    for card in cards:
        by_package.setdefault(card.package, []).append(card.name)

    statuses: list[McpServerStatus] = []
    with _lock:
        errors = dict(_server_errors)
        servers_snapshot = dict(_servers)

    for name, cfg in configs.items():
        pkg = f"mcp-{name}"
        tool_names = by_package.get(pkg, [])
        server = servers_snapshot.get(name)
        is_connected = _server_is_live(server) and bool(tool_names)
        error = None if is_connected else errors.get(name)
        if not is_connected and not error:
            if server_connect_gave_up(name):
                error = errors.get(name) or f"连接失败，已重试 {_MAX_CONNECT_ATTEMPTS} 次并暂停"
            elif server is None:
                error = "未连接（启动时未发现，正在重试或请检查网络/URL）"
            elif not _server_is_live(server):
                error = server.error or "会话已断开"
            elif not tool_names:
                error = "已连接但未注册工具"
        statuses.append(
            McpServerStatus(
                name=name,
                connected=is_connected,
                transport=cfg.transport,
                tool_count=len(tool_names),
                tool_names=tool_names,
                error=error,
                supports_parallel_tool_calls=cfg.supports_parallel_tool_calls,
            )
        )
    with _lock:
        _mcp_status_cache = copy.deepcopy(statuses)
        _mcp_status_cache_at = time.monotonic()
    return statuses


# 不在 import 时自动连接：避免 uvicorn reload 父子进程重复 discover 卡住启动。
# 由 FastAPI lifespan / 工具调用按需触发 ensure_mcp_connected。
