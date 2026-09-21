from __future__ import annotations

import json
import logging
import time

from app.tools.card import ToolCard, make_card
from app.tools.mcp.bridge import get_mcp_cards
from app.tools.mcp.config import load_mcp_catalog
from app.tools.mcp.lifecycle import get_mcp_status_snapshot
from app.tools.mcp.types import transport_to_canonical

logger = logging.getLogger(__name__)

_MAX_STATUS_TOOL_NAMES = 40
_MAX_REGISTERED_TOOLS = 40
_MAX_TOOL_SUMMARY_CHARS = 160
_MAX_SERVERS = 40
_MAX_ERROR_CHARS = 240


def mcp_status_handler(**_kwargs) -> str:
    """返回 MCP Server 与已注册工具的 JSON 快照（供模型规划 tool_search）。"""
    try:
        payload = build_mcp_status_payload()
        logger.debug(
            "MCP status snapshot requested session=%s run=%s tool_call_id=%s stale=%s source=%s",
            _kwargs.get("session_id"),
            _kwargs.get("run_id"),
            _kwargs.get("tool_call_id"),
            payload.get("snapshot_stale"),
            payload.get("snapshot_source"),
        )
    except Exception as exc:
        # 状态查询失败不应让整轮工具调用异常退出，也不向客户端泄漏堆栈。
        logger.exception("MCP 状态查询失败")
        payload = {
            "error": "mcp_status_unavailable",
            "message": f"MCP 状态暂时不可用: {type(exc).__name__}",
            "configured_servers": [],
            "servers": [],
            "registered_tools": [],
            "registered_tools_total": 0,
            "registered_tools_truncated": False,
            "config_revision": 0,
            "servers_truncated": False,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_mcp_status_payload() -> dict:
    snapshot = get_mcp_status_snapshot()
    statuses = snapshot.statuses
    catalog = load_mcp_catalog()
    remote_cards = get_mcp_cards()
    servers_payload = []
    for s in statuses[:_MAX_SERVERS]:
        transport = (
            s.normalized_transport
            or transport_to_canonical(s.transport)
        )
        error = (s.error or "")[:_MAX_ERROR_CHARS] if s.error else None
        servers_payload.append(
            {
                "name": s.name,
                "id": s.name,
                "display_name": s.display_name or s.name,
                "enabled": s.enabled,
                "connected": s.connected,
                "transport": s.transport.value,
                "normalized_transport": transport,
                "state": s.state,
                "tool_count": s.tool_count,
                "tools": list(s.tool_names[:_MAX_STATUS_TOOL_NAMES]),
                "tools_truncated": len(s.tool_names) > _MAX_STATUS_TOOL_NAMES,
                "error": error,
                "error_truncated": bool(s.error and len(s.error) > _MAX_ERROR_CHARS),
                "error_code": s.error_code,
                "config_revision": s.config_revision or snapshot.config_revision,
                "can_connect": s.can_connect,
            }
        )
    return {
        "configured_servers": list(catalog.servers.keys()),
        "servers": servers_payload,
        "servers_total": len(statuses),
        "servers_truncated": len(statuses) > _MAX_SERVERS,
        "registered_tools": [
            {
                "name": c.name,
                "package": c.package,
                "summary": (c.summary or "")[:_MAX_TOOL_SUMMARY_CHARS],
            }
            for c in remote_cards[:_MAX_REGISTERED_TOOLS]
        ],
        "registered_tools_total": len(remote_cards),
        "registered_tools_truncated": len(remote_cards) > _MAX_REGISTERED_TOOLS,
        "snapshot_captured_at": snapshot.captured_at,
        "snapshot_age_seconds": max(0.0, time.time() - snapshot.captured_at) if snapshot.captured_at else 0.0,
        "snapshot_stale": snapshot.stale,
        "snapshot_source": snapshot.source,
        "config_revision": snapshot.config_revision or catalog.revision,
        "applied_revision": snapshot.applied_revision,
        "usage_hint": (
            "MCP 远程工具与内置延时工具同级：tool_search(query=能力关键词) "
            "→ tool_describe(name) → tool_call(name, arguments)。"
            "搜索可用「bing」「网络搜索」等关键词，无需先查本工具。"
            "仅排查连接问题时再调用 mcp_status。"
        ),
    }


def format_mcp_status_for_model() -> str:
    payload = build_mcp_status_payload()
    if not payload["configured_servers"]:
        return ""
    lines = ["### MCP 外部工具", ""]
    for srv in payload["servers"]:
        state = srv.get("state") or ("已连接" if srv["connected"] else f"未连接（{srv.get('error') or '未知'}）")
        tools = "、".join(srv["tools"]) if srv["tools"] else "（无）"
        lines.append(f"- **{srv['name']}** [{state}]：{tools}")
    lines.append("")
    lines.append(payload["usage_hint"])
    return "\n".join(lines)


def build_mcp_status_card() -> ToolCard:
    return make_card(
        package="mcp",
        name="mcp_status",
        handler=mcp_status_handler,
        summary="查看 MCP Server 连接状态与已注册外部工具列表（排查用）",
        description="""【延时工具·排查专用】查询外部 MCP Server 的连接状态与已发现工具名。

适用场景：
- 用户明确询问 MCP 是否连接、有哪些外部工具
- MCP 工具调用失败，需要确认 Server 状态与工具名

不适用场景：
- 用户要求搜索、查询信息 → 直接 tool_search「bing 搜索」等，再 describe + call
- 普通任务前「确认一下 MCP」→ 不需要，MCP 服务与文件系统等包同级

返回：JSON，含 configured_servers、servers（连接状态与 tool 名）、registered_tools、usage_hint。

调用方式：tool_search(query="mcp 状态") → tool_describe(name="mcp_status") → tool_call。""",
        display_name="MCP 状态",
        display_icon="🔌",
        parameters={"type": "object", "properties": {}},
        risk_level="safe",
        concurrency="safe",
        source="plugin",
    )
