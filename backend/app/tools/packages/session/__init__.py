from pathlib import Path

from app.tools.card import ToolCard, make_card
from app.tools.packages.base import ToolPackage
from app.tools.packages.session.descriptions import TOOL_DOCS
from app.tools.packages.session import handlers as session_handlers


class SessionPackage(ToolPackage):
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        todo_doc = TOOL_DOCS["todo"]
        ask_doc = TOOL_DOCS["ask_user"]
        return [
            make_card(
                package="session",
                name="todo",
                handler=session_handlers.todo,
                summary=todo_doc.summary,
                description=todo_doc.description,
                display_name="待办",
                display_icon="✅",
            ),
            make_card(
                package="session",
                name="ask_user",
                handler=session_handlers.ask_user,
                summary=ask_doc.summary,
                description=ask_doc.description,
                display_name="询问用户",
                display_icon="❓",
            ),
        ]


PACKAGE = SessionPackage(Path(__file__).parent)
