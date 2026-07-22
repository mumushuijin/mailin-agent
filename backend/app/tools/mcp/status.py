from __future__ import annotations

import json

from app.tools.card import ToolCard, make_card
from app.tools.mcp.bridge import get_mcp_cards
from app.tools.mcp.config import load_mcp_server_configs
from app.tools.mcp.lifecycle import get_mcp_status


def mcp_status_handler(**_kwargs) -> str:
    """返回 MCP Server 与已注册工具的 JSON 快照（供模型规划 tool_search）。"""
    return json.dumps(build_mcp_status_payload(), ensure_ascii=False, indent=2)


def build_mcp_status_payload() -> dict:
    statuses = get_mcp_status()
    configured = load_mcp_server_configs()
    remote_cards = get_mcp_cards()
    return {
        "configured_servers": list(configured.keys()),
        "servers": [
            {
                "name": s.name,
                "connected": s.connected,
                "transport": s.transport.value,
                "tool_count": s.tool_count,
                "tools": s.tool_names,
                "error": s.error,
            }
            for s in statuses
        ],
        "registered_tools": [
            {
                "name": c.name,
                "package": c.package,
                "summary": c.summary,
            }
            for c in remote_cards
        ],
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
        state = "已连接" if srv["connected"] else f"未连接（{srv.get('error') or '未知'}）"
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
        source="plugin",
    )
