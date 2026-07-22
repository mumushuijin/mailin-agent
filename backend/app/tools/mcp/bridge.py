from __future__ import annotations

import re
from typing import Any, Callable

from app.tools.card import ToolCard, make_card
from app.tools.mcp.types import McpServerConfig

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def sanitize_mcp_name_component(value: str) -> str:
    return _NAME_SAFE_RE.sub("_", str(value or ""))


def prefixed_tool_name(server_name: str, tool_name: str) -> str:
    safe_server = sanitize_mcp_name_component(server_name)
    safe_tool = sanitize_mcp_name_component(tool_name)
    return f"mcp_{safe_server}_{safe_tool}"


def normalize_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """将 MCP inputSchema 规范化为 JSON Schema object（供 ToolCard.parameters）。"""
    if not schema or not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    normalized = dict(schema)
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized["properties"] = {}
    return normalized


def mcp_tool_to_card(
    server_name: str,
    *,
    tool_name: str,
    description: str,
    input_schema: dict[str, Any] | None,
    handler: Callable[..., str],
    config: McpServerConfig,
) -> ToolCard:
    """将单个 MCP 工具投影为 ToolCard。"""
    public_name = prefixed_tool_name(server_name, tool_name)
    return make_card(
        package=f"mcp-{server_name}",
        name=public_name,
        handler=handler,
        summary=description or f"MCP 工具 {tool_name}",
        description=description or f"MCP 工具 {tool_name}（来自 server '{server_name}'）",
        display_name=f"MCP {tool_name}",
        display_icon="🔌",
        parameters=normalize_input_schema(input_schema),
        risk_level="moderate",
        requires_confirmation=False,
        sandbox_policy="external_mcp",
        source="plugin",
    )


# ---------------------------------------------------------------------------
# 内存注册表（lifecycle 写入，registry 读取）
# ---------------------------------------------------------------------------

_mcp_cards: list[ToolCard] = []
_mcp_card_by_name: dict[str, ToolCard] = {}


def set_mcp_cards(cards: list[ToolCard]) -> None:
    global _mcp_cards, _mcp_card_by_name
    _mcp_cards = list(cards)
    _mcp_card_by_name = {c.name: c for c in cards}


def get_mcp_cards() -> list[ToolCard]:
    return list(_mcp_cards)


def get_mcp_card(name: str) -> ToolCard | None:
    return _mcp_card_by_name.get(name)
