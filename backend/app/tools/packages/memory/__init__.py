from pathlib import Path

from app.context.memory.consolidator import consolidate_to_longterm, consolidate_user_request
from app.context.memory.grep import grep_daily_memories
from app.context.sediment import append_daily_memory
from app.core.settings import get_settings
from app.storage.workspace import MemoryStore
from app.tools.card import ToolCard, make_card
from app.tools.packages.base import ToolPackage
from app.tools.packages.memory.descriptions import TOOL_DOCS


def _workspace_root() -> Path:
    return get_settings().workspace_path


def _memory_list() -> str:
    store = MemoryStore(_workspace_root())
    entries = store.list_entries()
    if not entries:
        return "暂无每日记忆"
    return "\n".join(f"- {e['date']}: {e['preview']}" for e in entries)


def _memory_get(filename: str) -> str:
    store = MemoryStore(_workspace_root())
    try:
        return store.get(filename)["content"]
    except Exception as e:
        return str(e)


def _memory_grep(keyword: str) -> str:
    return grep_daily_memories(keyword, _workspace_root())


def _memory_add(content: str) -> str:
    workspace = _workspace_root()
    path = append_daily_memory(content, workspace, source="agent")
    return f"已追加到 {path.name}"


def _memory_consolidate(
    fact: str | None = None,
    target: str = "memory",
    section: str = "misc",
) -> str:
    workspace = _workspace_root()
    if fact and fact.strip():
        t = target if target in ("memory", "user") else "memory"
        return consolidate_user_request(fact.strip(), workspace, target=t, section=section)
    return consolidate_to_longterm(workspace)


def _card(name: str, handler, *, risk_level="safe", parameters=None):
    doc = TOOL_DOCS[name]
    return make_card(
        package="memory",
        name=name,
        handler=handler,
        summary=doc.summary,
        description=doc.description,
        display_name=_DISPLAY_NAMES[name],
        display_icon=_DISPLAY_ICONS[name],
        risk_level=risk_level,
        parameters=parameters,
    )


_DISPLAY_NAMES = {
    "memory_list": "列出记忆文件",
    "memory_get": "读取记忆",
    "memory_grep": "搜索每日记忆",
    "memory_add": "添加记忆",
    "memory_consolidate": "写入长期记忆",
}

_DISPLAY_ICONS = {
    "memory_list": "📋",
    "memory_get": "📖",
    "memory_grep": "🔍",
    "memory_add": "📝",
    "memory_consolidate": "🗂️",
}


class MemoryPackage(ToolPackage):
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        return [
            _card("memory_list", _memory_list),
            _card("memory_get", _memory_get),
            _card("memory_grep", _memory_grep),
            _card("memory_add", _memory_add, risk_level="moderate"),
            _card(
                "memory_consolidate",
                _memory_consolidate,
                risk_level="moderate",
                parameters={
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "用户要记住的事实正文（「请记住」场景必填）；省略则批量沉淀温层",
                        },
                        "target": {
                            "type": "string",
                            "enum": ["memory", "user"],
                            "description": "写入目标文件，默认 memory",
                        },
                        "section": {
                            "type": "string",
                            "description": "固定节 key（如 preference、identity）；不确定用 misc",
                        },
                    },
                },
            ),
        ]


PACKAGE = MemoryPackage(Path(__file__).parent)
