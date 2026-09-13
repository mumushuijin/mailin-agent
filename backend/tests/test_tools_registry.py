from __future__ import annotations

import pytest

from app.tools.card import make_card
from app.tools.exposure import to_langchain_tool, to_langchain_tools
from app.tools.packages import BUILTIN_PACKAGES
from app.tools.registry import ToolRegistry, clear_tools_cache, get_tools


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    clear_tools_cache()
    yield
    clear_tools_cache()


def test_builtin_packages_have_unique_tool_ids():
    ids: set[str] = set()
    for package in BUILTIN_PACKAGES:
        for card in package.build_cards():
            assert card.id not in ids, f"duplicate tool id: {card.id}"
            ids.add(card.id)


def test_registry_respects_config_switches():
    config = {
        "tools": {
            "filesystem": True,
            "memory": False,
            "calculator": True,
            "web_search": False,
        }
    }
    registry = ToolRegistry(config=config)
    cards = registry.resolve_cards()
    names = {card.name for card in cards}

    assert "read_file" in names
    assert "python_calculator" in names
    assert "memory_list" not in names
    assert "web_search" not in names


def test_registry_enables_web_search_when_configured():
    config = {
        "tools": {
            "filesystem": False,
            "memory": False,
            "calculator": False,
            "datetime": False,
            "web_search": True,
            "shell": False,
            "session": False,
            "skills": False,
        }
    }
    registry = ToolRegistry(config=config)
    names = {card.name for card in registry.resolve_cards()}
    assert names == {"web_search"}


def test_tool_card_projects_to_langchain():
    def echo(text: str) -> str:
        return text

    card = make_card(
        package="demo",
        name="echo",
        handler=echo,
        summary="echo",
        description="echo text",
        display_name="Echo",
        display_icon="🔁",
    )
    tool = to_langchain_tool(card)
    assert tool.name == "echo"
    assert tool.invoke({"text": "hi"}) == "hi"


def test_get_tools_returns_langchain_tools():
    tools = get_tools()
    assert len(tools) >= 8
    tool_names = {tool.name for tool in tools}
    assert "read_file" in tool_names
    # tool_search 启用时延时工具经桥接暴露，不直接 bind
    assert "glob_search" not in tool_names or "tool_search" not in tool_names
    if "tool_search" in tool_names:
        assert "memory_list" not in tool_names
    else:
        assert "glob_search" in tool_names


def test_web_search_formats_results(monkeypatch):
    from app.tools.packages.web import search as search_module
    from app.tools.packages.web.search import SearchResult, search_web

    monkeypatch.setattr(
        search_module,
        "_search_tavily",
        lambda query, max_results=5: [
            SearchResult(title="Rust 2024", url="https://example.com/rust", snippet="Edition 2024")
        ],
    )
    monkeypatch.setattr(
        search_module,
        "get_settings",
        lambda: type("S", (), {"tavily_api_key": "test"})(),
    )

    result = search_web("rust 2024")
    assert "Rust 2024" in result
    assert "https://example.com/rust" in result
    assert "Edition 2024" in result
    assert "tavily" in result
