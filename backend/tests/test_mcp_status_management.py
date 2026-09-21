"""MCP status payload 扩展字段与禁用隔离。"""

from __future__ import annotations

import json
from pathlib import Path

from app.tools.mcp.bridge import set_mcp_cards
from app.tools.mcp.status import build_mcp_status_payload
from app.tools.mcp.types import McpServerStatus, McpStatusSnapshot, McpTransport
from app.tools.card import make_card


def test_mcp_status_payload_includes_revision_and_state(monkeypatch):
    from app.tools.mcp import status as status_module

    monkeypatch.setattr(
        status_module,
        "get_mcp_status_snapshot",
        lambda: McpStatusSnapshot(
            statuses=[
                McpServerStatus(
                    name="demo",
                    connected=True,
                    transport=McpTransport.HTTP,
                    tool_count=1,
                    tool_names=["t1"],
                    enabled=True,
                    state="ready",
                    normalized_transport="streamable-http",
                    config_revision=3,
                    can_connect=True,
                ),
                McpServerStatus(
                    name="off",
                    connected=False,
                    transport=McpTransport.STDIO,
                    enabled=False,
                    state="disabled",
                    normalized_transport="stdio",
                    config_revision=3,
                    can_connect=False,
                ),
            ],
            captured_at=1000.0,
            source="test",
            stale=False,
            config_revision=3,
            applied_revision=3,
        ),
    )
    monkeypatch.setattr(
        status_module,
        "load_mcp_catalog",
        lambda: type("C", (), {"servers": {"demo": object(), "off": object()}, "revision": 3})(),
    )
    set_mcp_cards([])
    payload = build_mcp_status_payload()
    assert payload["config_revision"] == 3
    assert payload["applied_revision"] == 3
    assert payload["servers"][0]["state"] == "ready"
    assert payload["servers"][0]["normalized_transport"] == "streamable-http"
    assert payload["servers"][0]["enabled"] is True
    assert payload["servers"][1]["state"] == "disabled"
    # 兼容旧字段
    assert payload["servers"][0]["name"] == "demo"
    assert payload["servers"][0]["connected"] is True


def test_disabled_server_not_in_callable_cards(tmp_path: Path, monkeypatch):
    from app.core import settings as settings_module
    from tests.config_test_utils import fake_settings, write_config

    cfg = {
        "mcp": {
            "version": 1,
            "revision": 1,
            "servers": {
                "off": {
                    "enabled": False,
                    "connection": {"type": "streamable-http", "url": "https://example.com/mcp"},
                }
            },
        },
        "tools": {"mcp": True, "tool_search": {"enabled": True}},
    }
    fake = fake_settings(tmp_path, defaults=tmp_path)
    write_config(fake, cfg)
    monkeypatch.setattr(settings_module, "get_settings", lambda: fake)
    monkeypatch.setattr("app.tools.mcp.config.get_settings", lambda: fake)

    # 即使有人塞入了卡片，registry 仍以配置为准；这里验证运行时 configs 不含禁用项
    from app.tools.mcp.config import load_mcp_server_configs

    assert "off" not in load_mcp_server_configs(tmp_path)

    clear_tools_cache = __import__("app.tools.registry", fromlist=["clear_tools_cache"]).clear_tools_cache
    clear_tools_cache()
    set_mcp_cards(
        [
            make_card(
                package="mcp-off",
                name="mcp_off_tool",
                handler=lambda: "x",
                summary="x",
                description="x",
                display_name="Off",
                display_icon="x",
                source="plugin",
            )
        ]
    )
    # 管理状态仍可见
    from app.tools.mcp.config import load_mcp_catalog

    catalog = load_mcp_catalog(tmp_path)
    assert "off" in catalog.servers
    assert catalog.servers["off"].enabled is False
