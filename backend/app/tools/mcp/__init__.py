"""MCP Client：连接外部 MCP Server，投影为 ToolCard。

公开 API（lifecycle 模块实现后生效）：
    discover_mcp_servers()  — 启动时发现并注册 MCP 工具
    shutdown_mcp_servers()  — 关闭所有连接与子进程
    reload_mcp_servers()    — 重载配置
    get_mcp_status()        — 连接状态摘要
    get_mcp_cards()         — 当前已注册的 MCP ToolCard 列表
"""

from __future__ import annotations

from app.tools.mcp.bridge import get_mcp_cards
from app.tools.mcp.lifecycle import (
    discover_mcp_servers,
    ensure_mcp_connected,
    get_mcp_server,
    get_mcp_status,
    get_mcp_status_snapshot,
    reload_mcp_servers,
    shutdown_mcp_servers,
)
from app.tools.mcp.status import build_mcp_status_payload

__all__ = [
    "discover_mcp_servers",
    "ensure_mcp_connected",
    "shutdown_mcp_servers",
    "reload_mcp_servers",
    "get_mcp_status",
    "get_mcp_status_snapshot",
    "get_mcp_cards",
    "get_mcp_server",
    "build_mcp_status_payload",
]
