from pathlib import Path

from app.tools.card import ToolCard, make_card
from app.tools.packages.base import ToolPackage
from app.tools.packages.datetime.descriptions import TOOL_DOCS
from app.tools.packages.datetime.time import get_current_time


class DatetimePackage(ToolPackage):
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        doc = TOOL_DOCS["get_current_time"]
        return [
            make_card(
                package="datetime",
                name="get_current_time",
                handler=get_current_time,
                summary=doc.summary,
                description=doc.description,
                display_name="当前时间",
                display_icon="🕐",
                parameters={
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "可选。IANA 时区（如 Asia/Shanghai）或 UTC 偏移（如 UTC+8）",
                        }
                    },
                },
            ),
        ]


PACKAGE = DatetimePackage(Path(__file__).parent)
