from __future__ import annotations

import json

import pytest

from app.tools.registry import ToolRegistry, clear_tools_cache, get_tools
from app.tools.tool_search import (
    TOOL_CALL_NAME,
    TOOL_DESCRIBE_NAME,
    TOOL_SEARCH_NAME,
    ToolSearchConfig,
    assemble_bind_tools,
    dispatch_tool_describe,
    dispatch_tool_search,
    execute_single_tool_call,
    load_tool_search_config,
)


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    clear_tools_cache()
    yield
    clear_tools_cache()


def _base_config(**tool_search_overrides) -> dict:
    ts = {
        "enabled": True,
        "hot_tools": [
            "read_file",
            "list_directory",
            "memory_grep",
            "memory_add",
            "python_calculator",
        ],
        "search_default_limit": 5,
        "max_search_limit": 20,
    }
    ts.update(tool_search_overrides)
    return {
        "tools": {
            "filesystem": True,
            "memory": True,
            "calculator": True,
            "web_search": False,
            "tool_search": ts,
        }
    }


def test_assemble_bind_tools_splits_hot_and_bridge():
    registry = ToolRegistry(config=_base_config())
    cards = registry.resolve_cards()
    result = assemble_bind_tools(cards, ToolSearchConfig.from_config(_base_config()))

    names = {t.name for t in result.tools}
    assert result.activated is True
    assert "read_file" in names
    assert "memory_list" not in names
    assert TOOL_SEARCH_NAME in names
    assert TOOL_DESCRIBE_NAME in names
    assert TOOL_CALL_NAME in names


def test_tool_search_disabled_passthrough():
    config = _base_config()
    config["tools"]["tool_search"] = {"enabled": False}
    registry = ToolRegistry(config=config)
    cards = registry.resolve_cards()
    result = assemble_bind_tools(cards, ToolSearchConfig.from_config(config))

    names = {t.name for t in result.tools}
    assert result.activated is False
    assert "memory_list" in names
    assert TOOL_SEARCH_NAME not in names


def test_dispatch_tool_search_finds_memory_tools():
    registry = ToolRegistry(config=_base_config())
    cards = registry.resolve_cards()
    config = ToolSearchConfig.from_config(_base_config())

    raw = dispatch_tool_search(
        {"query": "列出记忆文件"},
        cards=cards,
        config=config,
    )
    payload = json.loads(raw)
    match_names = {m["name"] for m in payload["matches"]}
    assert "memory_list" in match_names
    assert payload["total_available"] > 0


def test_dispatch_tool_describe_returns_card():
    registry = ToolRegistry(config=_base_config())
    cards = registry.resolve_cards()
    config = ToolSearchConfig.from_config(_base_config())

    raw = dispatch_tool_describe(
        {"name": "write_file"},
        cards=cards,
        config=config,
    )
    payload = json.loads(raw)
    assert payload["name"] == "write_file"
    assert "parameters" in payload
    assert "summary" in payload


def test_tool_call_rejects_hot_tools():
    registry = ToolRegistry(config=_base_config())
    cards = registry.resolve_cards()
    config = ToolSearchConfig.from_config(_base_config())

    _, content = execute_single_tool_call(
        TOOL_CALL_NAME,
        {"name": "read_file", "arguments": {"file_path": "x.md"}},
        cards=cards,
        config=config,
    )
    payload = json.loads(content)
    assert "error" in payload


def test_get_tools_respects_tool_search():
    registry = ToolRegistry(config=_base_config())
    clear_tools_cache()
    from app.tools import registry as registry_mod

    original = registry_mod.get_registry
    registry_mod.get_registry = lambda: registry  # type: ignore[assignment]
    try:
        names = {t.name for t in get_tools()}
        assert "glob_search" not in names
        assert TOOL_SEARCH_NAME in names
        assert "read_file" in names
    finally:
        registry_mod.get_registry = original
        clear_tools_cache()


def test_load_tool_search_config_defaults():
    cfg = load_tool_search_config({"tools": {}})
    assert cfg.enabled is True
    assert "read_file" in cfg.hot_tools
    assert "glob_search" in cfg.hot_tools
    assert "todo" in cfg.hot_tools
    assert "python_calculator" not in cfg.hot_tools
    assert cfg.mcp_as_hot is False


def test_legacy_hot_tools_are_upgraded():
    cfg = load_tool_search_config(
        {
            "tools": {
                "tool_search": {
                    "hot_tools": [
                        "read_file",
                        "list_directory",
                        "run_shell",
                        "memory_grep",
                        "memory_add",
                        "memory_consolidate",
                        "python_calculator",
                        "get_current_time",
                    ]
                }
            }
        }
    )
    assert "glob_search" in cfg.hot_tools
    assert "todo" in cfg.hot_tools
    assert "python_calculator" not in cfg.hot_tools


def test_custom_hot_tools_preserved():
    cfg = load_tool_search_config(
        {"tools": {"tool_search": {"hot_tools": ["read_file", "web_search"]}}}
    )
    assert cfg.hot_tools == ("read_file", "web_search")


def test_mcp_tools_deferred_and_searchable():
    from app.tools.card import make_card

    mcp_card = make_card(
        package="mcp-bing",
        name="mcp_bing_cn_mcp_server_bing_search",
        handler=lambda query: "ok",
        summary="Bing 搜索",
        description="搜索网页",
        display_name="Bing",
        display_icon="🔍",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        source="plugin",
    )
    builtin = ToolRegistry(config=_base_config()).resolve_cards()
    cards = builtin + [mcp_card]
    config = ToolSearchConfig.from_config(_base_config())
    result = assemble_bind_tools(cards, config)
    names = {t.name for t in result.tools}
    assert "mcp_bing_cn_mcp_server_bing_search" not in names
    assert "web_search" not in names
    assert TOOL_SEARCH_NAME in names

    raw = dispatch_tool_search(
        {"query": "bing 网络搜索"},
        cards=cards,
        config=config,
    )
    payload = json.loads(raw)
    match_names = {m["name"] for m in payload["matches"]}
    assert "mcp_bing_cn_mcp_server_bing_search" in match_names
