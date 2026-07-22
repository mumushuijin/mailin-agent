from __future__ import annotations

from app.tools.card import ToolCard
from app.tools.registry import get_registry


def find_tool_card(tool_name: str) -> ToolCard | None:
    for card in get_registry().resolve_cards():
        if card.name == tool_name:
            return card
    return None


def needs_approval(tool_name: str) -> bool:
    card = find_tool_card(tool_name)
    if not card:
        return False
    return card.requires_confirmation or card.risk_level == "dangerous"
