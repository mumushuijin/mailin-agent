from __future__ import annotations

from pathlib import Path

from app.tools.card import ToolCard
from app.tools.mcp.bridge import get_mcp_cards
from app.tools.mcp.status import build_mcp_status_card
from app.tools.packages.base import ToolPackage


class McpToolPackage(ToolPackage):
    """MCP 工具包：与 packages/* 同级，内部由 lifecycle 管理连接与发现。"""

    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        cards: list[ToolCard] = [build_mcp_status_card()]
        cards.extend(get_mcp_cards())
        return cards


PACKAGE = McpToolPackage(Path(__file__).parent)
