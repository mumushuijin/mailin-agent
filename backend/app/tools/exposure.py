from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from app.tools.card import ToolCard


def _infer_parameters(card: ToolCard) -> dict:
    if card.parameters:
        return card.parameters
    tool = StructuredTool.from_function(
        func=card.handler,
        name=card.name,
        description=card.description,
    )
    if tool.args_schema is not None:
        return tool.args_schema.model_json_schema()
    return {"type": "object", "properties": {}}


def enrich_card_parameters(cards: list[ToolCard]) -> list[ToolCard]:
    for card in cards:
        if not card.parameters:
            card.parameters = _infer_parameters(card)
    return cards


def to_langchain_tool(card: ToolCard, *, description: str | None = None) -> BaseTool:
    return StructuredTool.from_function(
        func=card.handler,
        name=card.name,
        description=description if description is not None else card.description,
    )


def to_langchain_tools(cards: list[ToolCard]) -> list[BaseTool]:
    return [to_langchain_tool(card) for card in cards if card.enabled]
