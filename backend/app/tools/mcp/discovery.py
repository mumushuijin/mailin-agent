from __future__ import annotations

import logging
from typing import Any

from app.tools.card import ToolCard
from app.tools.mcp.bridge import mcp_tool_to_card
from app.tools.mcp.handlers import make_call_tool_handler
from app.tools.mcp.server_task import MCPServerTask
from app.tools.mcp.types import McpServerConfig

logger = logging.getLogger(__name__)


def build_cards_for_server(
    server: MCPServerTask,
    config: McpServerConfig,
    *,
    get_server,
) -> list[ToolCard]:
    """将已连接 Server 的 tools 列表投影为 ToolCard。"""
    cards: list[ToolCard] = []
    for mcp_tool in server.tools:
        tool_name = getattr(mcp_tool, "name", None)
        if not tool_name:
            continue
        if not config.tools_filter.should_register(str(tool_name)):
            logger.debug(
                "MCP server '%s': 跳过工具 '%s'（配置过滤）",
                server.name,
                tool_name,
            )
            continue

        description = getattr(mcp_tool, "description", None) or ""
        input_schema = getattr(mcp_tool, "inputSchema", None)
        if input_schema is not None and hasattr(input_schema, "model_dump"):
            input_schema = input_schema.model_dump()
        elif input_schema is not None and not isinstance(input_schema, dict):
            input_schema = dict(input_schema) if input_schema else None

        handler = make_call_tool_handler(
            server.name,
            str(tool_name),
            get_server=get_server,
            timeout=config.timeout,
        )
        cards.append(
            mcp_tool_to_card(
                server.name,
                tool_name=str(tool_name),
                description=str(description),
                input_schema=input_schema,
                handler=handler,
                config=config,
            )
        )
    return cards
