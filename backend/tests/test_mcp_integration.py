"""端到端：用户问「请用 bing 搜索 mcp 是什么」时，tool_search 应能发现 MCP 工具。"""

from __future__ import annotations

import json

import pytest

from app.tools.card import make_card
from app.tools.context import build_tools_disclosure_context
from app.tools.mcp.status import build_mcp_status_payload
from app.tools.registry import ToolRegistry, clear_tools_cache
from app.tools.tool_search import (
    TOOL_SEARCH_NAME,
    ToolSearchConfig,
    assemble_bind_tools,
    classify_cards,
    dispatch_tool_search,
)


def _config_with_mcp_card():
    return {
        "mcp_servers": {"bing": {"url": "https://example.com/mcp"}},
        "tools": {
            "filesystem": True,
            "memory": True,
            "calculator": True,
            "web_search": False,
            "mcp": True,
            "tool_search": {"enabled": True, "mcp_as_hot": False},
        },
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_tools_cache()
    yield
    clear_tools_cache()


def test_mcp_package_in_registry():
    cfg = _config_with_mcp_card()
    mcp_card = make_card(
        package="mcp-bing",
        name="mcp_bing_cn_mcp_server_bing_search",
        handler=lambda query: json.dumps({"ok": True}),
        summary="Bing 搜索",
        description="搜索",
        display_name="Bing",
        display_icon="🔍",
        source="plugin",
    )
    from app.tools.mcp.bridge import set_mcp_cards

    set_mcp_cards([mcp_card])
    registry = ToolRegistry(config=cfg)
    names = {c.name for c in registry.resolve_cards()}
    assert "mcp_status" in names
    assert "mcp_bing_cn_mcp_server_bing_search" in names


def test_bind_tools_excludes_mcp_status_and_remote_tools():
    cfg = _config_with_mcp_card()
    mcp_card = make_card(
        package="mcp-bing",
        name="mcp_bing_cn_mcp_server_bing_search",
        handler=lambda query: "ok",
        summary="Bing 搜索",
        description="搜索",
        display_name="Bing",
        display_icon="🔍",
        source="plugin",
    )
    from app.tools.mcp.bridge import set_mcp_cards

    set_mcp_cards([mcp_card])
    registry = ToolRegistry(config=cfg)
    cards = registry.resolve_cards()
    asm = assemble_bind_tools(cards, ToolSearchConfig.from_config(cfg))
    names = {t.name for t in asm.tools}
    assert "mcp_status" not in names
    assert "mcp_bing_cn_mcp_server_bing_search" not in names
    assert TOOL_SEARCH_NAME in names


def test_tool_search_finds_bing_for_user_query():
    cfg = _config_with_mcp_card()
    mcp_card = make_card(
        package="mcp-bing",
        name="mcp_bing_cn_mcp_server_bing_search",
        handler=lambda query: "ok",
        summary="使用必应搜索引擎搜索信息",
        description="bing search",
        display_name="Bing",
        display_icon="🔍",
        source="plugin",
    )
    from app.tools.mcp.bridge import set_mcp_cards

    set_mcp_cards([mcp_card])
    registry = ToolRegistry(config=cfg)
    cards = registry.resolve_cards()
    ts = ToolSearchConfig.from_config(cfg)
    raw = dispatch_tool_search({"query": "bing 搜索 mcp"}, cards=cards, config=ts)
    matches = {m["name"] for m in json.loads(raw)["matches"]}
    assert "mcp_bing_cn_mcp_server_bing_search" in matches


def test_tools_disclosure_mentions_mcp_and_workflow():
    cfg = _config_with_mcp_card()
    mcp_card = make_card(
        package="mcp-bing",
        name="mcp_bing_cn_mcp_server_bing_search",
        handler=lambda query: "ok",
        summary="Bing",
        description="搜索",
        display_name="Bing",
        display_icon="🔍",
        source="plugin",
    )
    from app.tools.mcp.bridge import set_mcp_cards

    set_mcp_cards([mcp_card])
    text = build_tools_disclosure_context(cfg)
    assert "tool_search" in text
    assert "mcp_bing_cn_mcp_server_bing_search" in text or "bing" in text.lower()
    assert "勿为普通任务先查 mcp_status" in text


def test_mcp_status_deferred_and_searchable():
    cfg = _config_with_mcp_card()
    from app.tools.mcp.bridge import set_mcp_cards

    set_mcp_cards([])
    registry = ToolRegistry(config=cfg)
    cards = registry.resolve_cards()
    ts = ToolSearchConfig.from_config(cfg)
    _, deferred = classify_cards(cards, ts)
    deferred_names = {c.name for c in deferred}
    assert "mcp_status" in deferred_names

    raw = dispatch_tool_search({"query": "mcp 连接状态"}, cards=cards, config=ts)
    matches = {m["name"] for m in json.loads(raw)["matches"]}
    assert "mcp_status" in matches


def test_mcp_status_payload_shape():
    from app.tools.mcp.bridge import set_mcp_cards

    set_mcp_cards([])
    payload = build_mcp_status_payload()
    assert "usage_hint" in payload
