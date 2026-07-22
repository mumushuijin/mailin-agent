from pathlib import Path

from app.tools.card import ToolCard, make_card
from app.tools.packages.base import ToolPackage
from app.tools.packages.web.descriptions import TOOL_DOCS
from app.tools.packages.web.search import search_web


def _web_search(query: str) -> str:
    return search_web(query)


class WebPackage(ToolPackage):
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        doc = TOOL_DOCS["web_search"]
        return [
            make_card(
                package="web",
                name="web_search",
                handler=_web_search,
                summary=doc.summary,
                description=doc.description,
                display_name="网络搜索",
                display_icon="🌐",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词或自然语言查询，宜具体明确",
                        }
                    },
                    "required": ["query"],
                },
            ),
        ]


PACKAGE = WebPackage(Path(__file__).parent)
