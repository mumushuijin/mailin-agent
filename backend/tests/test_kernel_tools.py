from __future__ import annotations

from app.tools.registry import ToolRegistry
from app.tools.tool_search import KERNEL_NINE_TOOLS, ToolSearchConfig, assemble_bind_tools


def test_kernel_nine_are_hot_by_default():
    config = {
        "tools": {
            "filesystem": True,
            "shell": True,
            "session": True,
            "memory": True,
            "calculator": True,
            "datetime": True,
            "web_search": False,
            "mcp": False,
        }
    }
    registry = ToolRegistry(config=config)
    names = {t.name for t in assemble_bind_tools(registry.resolve_cards(), ToolSearchConfig.from_config(config)).tools}
    for name in KERNEL_NINE_TOOLS:
        assert name in names, name
    assert "python_calculator" not in names
    assert "web_search" not in names


def test_filesystem_disabled_hides_file_tools():
    config = {
        "tools": {
            "filesystem": False,
            "shell": True,
            "session": True,
            "memory": False,
            "calculator": False,
            "datetime": False,
            "web_search": False,
        }
    }
    names = {card.name for card in ToolRegistry(config=config).resolve_cards()}
    for name in ("read_file", "write_file", "replace_in_file", "glob_search", "search_files"):
        assert name not in names
    assert "run_shell" in names
    assert "todo" in names
    assert "ask_user" in names
