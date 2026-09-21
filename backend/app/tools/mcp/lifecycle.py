from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time

from app.tools.mcp.bridge import get_mcp_cards, set_mcp_cards
from app.tools.mcp.config import get_mcp_config_revision, load_mcp_catalog, load_mcp_server_configs
from app.tools.mcp.discovery import build_cards_for_server
from app.tools.mcp.loop import ensure_mcp_loop, run_on_mcp_loop, stop_mcp_loop
from app.tools.mcp.server_task import MCPServerTask
from app.tools.mcp.types import (
    McpCatalog,
    McpManagementState,
    McpServerConfig,
    McpServerDefinition,
    McpServerStatus,
    McpStatusSnapshot,
)

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
_mcp_status_snapshot: McpStatusSnapshot | None = None
_applied_config_revision: int = 0


def _invalidate_mcp_status_cache_locked() -> None:
    global _mcp_status_snapshot
    if _mcp_status_snapshot is not None:
        _mcp_status_snapshot.stale = True


def clear_mcp_status_cache() -> None:
    global _mcp_status_snapshot
    with _lock:
        _mcp_status_snapshot = None


def mark_mcp_config_stale(*, reason: str = "config") -> None:
    """配置变更后标记快照过期，不隐式触发 discovery。"""
    with _lock:
        _invalidate_mcp_status_cache_locked()
    logger.debug("MCP config marked stale: %s", reason)


def get_applied_config_revision() -> int:
    with _lock:
        return _applied_config_revision


def _set_applied_revision(revision: int) -> None:
    global _applied_config_revision
    with _lock:
        _applied_config_revision = int(revision)


def _clear_tools_cache_quietly() -> None:
    with _lock:
        _invalidate_mcp_status_cache_locked()
    try:
        from app.tools.registry import clear_tools_cache

        clear_tools_cache()
    except Exception:
        pass


def resolve_management_state(
    definition: McpServerDefinition | None,
    status: McpServerStatus | None,
    snapshot: McpStatusSnapshot | None = None,
) -> McpManagementState:
    """将配置与运行时快照映射为管理状态。"""
    if definition is not None and not definition.enabled:
        return "disabled"
    if definition is not None and definition.validation_errors:
        if not definition.command and not definition.url:
            return "not_configured"
        return "error"
    if definition is not None and not definition.can_connect:
        return "not_configured"

    config_revision = snapshot.config_revision if snapshot else get_mcp_config_revision()
    applied = snapshot.applied_revision if snapshot else get_applied_config_revision()
    if snapshot is not None and (snapshot.stale or config_revision != applied):
        # 配置已变更但尚未完成 refresh：优先 stale，除非正在 connecting
        if status is not None and status.state == "connecting":
            return "connecting"
        if not _MCP_SDK_AVAILABLE:
            return "error"
        return "stale"

    if not _MCP_SDK_AVAILABLE:
        return "error"

    if status is None:
        return "error"

    if status.state == "connecting":
        return "connecting"
    if status.connected and status.tool_count > 0:
        return "ready"
    if status.connected and status.tool_count == 0:
        return "degraded"
    if status.error:
        return "error"
    return "error"


def close_mcp_server(name: str) -> None:
    """关闭单个 Server 连接并移除其 ToolCard。"""
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
            logger.debug("MCP server '%s' close 异常: %s", name, exc)

    remaining = [c for c in get_mcp_cards() if c.package != f"mcp-{name}"]
    set_mcp_cards(remaining)
    _clear_tools_cache_quietly()
    _refresh_mcp_status_snapshot(source="close")


def test_mcp_server_connection(config: McpServerConfig) -> dict:
    """有界临时连接测试；成功也不替换正式运行时。"""
    if not _MCP_SDK_AVAILABLE:
        return {
            "ok": False,
            "state": "error",
            "tool_count": 0,
            "tool_names": [],
            "error": "未安装 mcp 包，请执行: uv pip install -e '.[mcp]'",
            "error_code": "mcp_extra_missing",
        }

    ensure_mcp_loop()

    async def _test() -> dict:
        server = MCPServerTask(f"__test__{config.name}", config, max_reconnect_attempts=1)
        try:
            await server.start()
            ready_timeout = min(config.connect_timeout, _DISCOVERY_READY_CAP_SEC)
            ready = await server.wait_ready(ready_timeout)
            if not ready:
                err = server.error or f"连接超时（{ready_timeout}s）"
                return {
                    "ok": False,
                    "state": "error",
                    "tool_count": 0,
                    "tool_names": [],
                    "error": err,
                    "error_code": "connect_timeout",
                }
            names = [str(getattr(t, "name", "")) for t in server.tools if getattr(t, "name", None)]
            names = [n for n in names if n]
            state: McpManagementState = "ready" if names else "degraded"
            return {
                "ok": True,
                "state": state,
                "tool_count": len(names),
                "tool_names": names,
                "error": None,
                "error_code": None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "state": "error",
                "tool_count": 0,
                "tool_names": [],
                "error": str(exc),
                "error_code": "protocol_error",
            }
        finally:
            try:
                await server.shutdown()
            except Exception:
                pass

    try:
        return run_on_mcp_loop(_test(), timeout=min(90.0, max(15.0, config.connect_timeout + 5)))
    except Exception as exc:
        return {
            "ok": False,
            "state": "error",
            "tool_count": 0,
            "tool_names": [],
            "error": str(exc),
            "error_code": "test_failed",
        }


def refresh_mcp_servers() -> dict[str, dict]:
    """基于已保存配置重建启用 Server；逐 Server 返回结果。"""
    _reset_connect_failures()
    reload_mcp_servers()
    catalog = load_mcp_catalog()
    _set_applied_revision(catalog.revision)
    snapshot = _refresh_mcp_status_snapshot(source="refresh")
    by_id = {s.name: s for s in snapshot.statuses}
    results: dict[str, dict] = {}
    for server_id, definition in catalog.servers.items():
        status = by_id.get(server_id)
        if not definition.enabled:
            results[server_id] = {
                "ok": True,
                "state": "disabled",
                "tool_count": 0,
                "error": None,
                "error_code": None,
            }
            continue
        if not definition.can_connect:
            results[server_id] = {
                "ok": False,
                "state": "error",
                "tool_count": 0,
                "error": "; ".join(definition.validation_errors) or "不可连接",
                "error_code": "validation_error",
            }
            continue
        ok = bool(status and status.connected)
        results[server_id] = {
            "ok": ok,
            "state": resolve_management_state(definition, status, snapshot),
            "tool_count": status.tool_count if status else 0,
            "error": None if ok else (status.error if status else "未连接"),
            "error_code": None if ok else (status.error_code if status else "not_connected"),
        }
    return results


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
        _refresh_mcp_status_snapshot(source="discovery")
        return []

    configs = load_mcp_server_configs()
    if not configs:
        set_mcp_cards([])
        _clear_tools_cache_quietly()
        _refresh_mcp_status_snapshot(source="discovery")
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
        try:
            _set_applied_revision(load_mcp_catalog().revision)
        except Exception:
            pass
        _refresh_mcp_status_snapshot(source="discovery")
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
    _refresh_mcp_status_snapshot(source="shutdown")


def _build_mcp_statuses(catalog: McpCatalog | None = None) -> list[McpServerStatus]:
    """从已提交的本地运行时状态构建快照，不触发任何连接动作。"""
    catalog = catalog if catalog is not None else load_mcp_catalog()
    cards = get_mcp_cards()
    by_package: dict[str, list[str]] = {}
    for card in cards:
        by_package.setdefault(card.package, []).append(card.name)

    statuses: list[McpServerStatus] = []
    discovering = _discovery_is_running()
    with _lock:
        errors = dict(_server_errors)
        servers_snapshot = dict(_servers)
        applied = _applied_config_revision

    config_revision = catalog.revision
    connectable_ids = {
        sid for sid, d in catalog.servers.items() if d.enabled and d.can_connect
    }
    for name, definition in catalog.servers.items():
        cfg = definition.to_runtime_config()
        pkg = f"mcp-{name}"
        tool_names = by_package.get(pkg, [])
        server = servers_snapshot.get(name)
        live = _server_is_live(server)
        is_connected = live and bool(tool_names)
        error = None if is_connected else errors.get(name)
        error_code = None
        if not definition.enabled:
            error = None
            error_code = "disabled"
        elif definition.validation_errors:
            error = "; ".join(definition.validation_errors)
            error_code = "validation_error"
        elif not _MCP_SDK_AVAILABLE:
            error = "未安装 mcp 包"
            error_code = "mcp_extra_missing"
        elif not is_connected and not error:
            if discovering and name in connectable_ids:
                error = None
                error_code = "connecting"
            elif server_connect_gave_up(name):
                error = errors.get(name) or f"连接失败，已重试 {_MAX_CONNECT_ATTEMPTS} 次并暂停"
                error_code = "connect_gave_up"
            elif server is None:
                error = "未连接（启动时未发现，正在重试或请检查网络/URL）"
                error_code = "not_connected"
            elif not live:
                error = server.error or "会话已断开"
                error_code = "disconnected"
            elif not tool_names:
                error = "已连接但未注册工具"
                error_code = "no_tools"

        if not definition.enabled:
            state: McpManagementState = "disabled"
        elif definition.validation_errors:
            state = "not_configured" if not (definition.command or definition.url) else "error"
        elif not _MCP_SDK_AVAILABLE:
            state = "error"
        elif discovering and definition.can_connect and definition.enabled and not is_connected:
            state = "connecting"
        elif config_revision != applied:
            state = "stale"
        elif is_connected and tool_names:
            state = "ready"
        elif live and not tool_names:
            state = "degraded"
        else:
            state = "error"

        statuses.append(
            McpServerStatus(
                name=name,
                connected=is_connected,
                transport=cfg.transport,
                tool_count=len(tool_names),
                tool_names=tool_names,
                error=error,
                supports_parallel_tool_calls=cfg.supports_parallel_tool_calls,
                enabled=definition.enabled,
                state=state,
                error_code=error_code,
                display_name=definition.display_name,
                normalized_transport=definition.connection_type,
                config_revision=config_revision,
                can_connect=definition.can_connect,
            )
        )
    return statuses


def _discovery_is_running() -> bool:
    with _discover_lock:
        return _discover_running


def _refresh_mcp_status_snapshot(*, source: str = "runtime") -> McpStatusSnapshot:
    global _mcp_status_snapshot
    catalog = load_mcp_catalog()
    statuses = _build_mcp_statuses(catalog)
    applied = get_applied_config_revision()
    stale = catalog.revision != applied
    snapshot = McpStatusSnapshot(
        statuses=copy.deepcopy(statuses),
        captured_at=time.time(),
        source=source,
        stale=stale,
        config_revision=catalog.revision,
        applied_revision=applied,
        captured_monotonic=time.monotonic(),
    )
    with _lock:
        _mcp_status_snapshot = snapshot
        return copy.deepcopy(snapshot)


def get_mcp_status_snapshot(*, force_refresh: bool = False) -> McpStatusSnapshot:
    """读取最近一次 MCP 状态快照，不启动 discovery 或重连。"""
    now = time.monotonic()
    with _lock:
        snapshot = copy.deepcopy(_mcp_status_snapshot)

    if snapshot is not None:
        age = now - snapshot.captured_monotonic
        if not force_refresh and age <= MCP_STATUS_CACHE_TTL_SECONDS and not snapshot.stale:
            return snapshot
        if _discovery_is_running() and not force_refresh:
            snapshot.stale = True
            return snapshot

    return _refresh_mcp_status_snapshot(source="local")


def get_mcp_status() -> list[McpServerStatus]:
    """返回各 Server 的最近已知连接状态，不触发连接动作。"""
    return get_mcp_status_snapshot().statuses


# 不在 import 时自动连接：避免 uvicorn reload 父子进程重复 discover 卡住启动。
# 由 FastAPI lifespan / 工具调用按需触发 ensure_mcp_connected。
