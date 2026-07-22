from pathlib import Path

from app.tools.card import ToolCard, make_card
from app.tools.packages.base import ToolPackage
from app.tools.packages.filesystem import handlers as fs
from app.tools.packages.filesystem.descriptions import TOOL_DOCS


def _card(name: str, handler, *, risk_level="safe", requires_confirmation=False):
    doc = TOOL_DOCS[name]
    return make_card(
        package="filesystem",
        name=name,
        handler=handler,
        summary=doc.summary,
        description=doc.description,
        display_name=_DISPLAY_NAMES[name],
        display_icon=_DISPLAY_ICONS[name],
        risk_level=risk_level,
        requires_confirmation=requires_confirmation,
        sandbox_policy="workspace_only",
    )


_DISPLAY_NAMES = {
    "read_file": "读取文件",
    "write_file": "写入文件",
    "replace_in_file": "替换文本",
    "list_directory": "列出目录",
    "search_files": "搜索文件内容",
    "glob_search": "Glob 搜索",
    "mkdir": "创建目录",
    "move_file": "移动文件",
    "delete_file": "删除文件",
}

_DISPLAY_ICONS = {
    "read_file": "📄",
    "write_file": "✏️",
    "replace_in_file": "📝",
    "list_directory": "📂",
    "search_files": "🔍",
    "glob_search": "🗂️",
    "mkdir": "📁",
    "move_file": "📦",
    "delete_file": "🗑️",
}


class FilesystemPackage(ToolPackage):
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        return [
            _card("read_file", fs.read_file),
            _card("write_file", fs.write_file, risk_level="moderate", requires_confirmation=True),
            _card("replace_in_file", fs.replace_in_file, risk_level="moderate", requires_confirmation=True),
            _card("list_directory", fs.list_directory),
            _card("search_files", fs.search_files),
            _card("glob_search", fs.glob_search),
            _card("mkdir", fs.mkdir),
            _card("move_file", fs.move_file, risk_level="moderate", requires_confirmation=True),
            _card("delete_file", fs.delete_file, risk_level="dangerous", requires_confirmation=True),
        ]


PACKAGE = FilesystemPackage(Path(__file__).parent)
