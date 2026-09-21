"""MCP 统一配置模型与兼容读取测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tools.mcp.config import (
    load_mcp_catalog,
    load_mcp_server_configs,
    merge_secret_maps,
    normalize_mcp_config,
    save_mcp_server,
)
from app.tools.mcp.types import MASK_LITERAL


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    from app.core import settings as settings_module
    from tests.config_test_utils import fake_settings, write_config

    fake = fake_settings(tmp_path)
    write_config(fake, {})
    monkeypatch.setattr(settings_module, "get_settings", lambda: fake)
    return tmp_path


def _write_config(workspace: Path, data: dict) -> None:
    from app.core.settings import get_settings

    settings = get_settings()
    path = Path(settings.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_canonical_shapes_stdio_http_sse(workspace: Path):
    _write_config(
        workspace,
        {
            "mcp": {
                "version": 1,
                "revision": 0,
                "servers": {
                    "local": {
                        "display_name": "Local",
                        "enabled": True,
                        "connection": {
                            "type": "stdio",
                            "command": "npx",
                            "args": ["-y", "demo"],
                            "env": {"FOO": "1"},
                        },
                        "timeouts": {"connect": 30, "call": 10},
                        "tools": {"include": [], "exclude": [], "resources": False, "prompts": False},
                        "runtime": {"supports_parallel_tool_calls": False},
                    },
                    "http_srv": {
                        "display_name": "HTTP",
                        "enabled": True,
                        "connection": {
                            "type": "streamable-http",
                            "url": "https://example.com/mcp",
                            "headers": {"Authorization": "Bearer ${TOKEN}"},
                        },
                    },
                    "sse_srv": {
                        "display_name": "SSE",
                        "enabled": True,
                        "connection": {
                            "type": "sse",
                            "url": "https://example.com/sse",
                        },
                    },
                },
            }
        },
    )
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("TOKEN", "secret")
    try:
        catalog = load_mcp_catalog(workspace)
    finally:
        monkeypatch_env.undo()

    assert catalog.version == 1
    assert catalog.servers["local"].connection_type == "stdio"
    assert catalog.servers["local"].command == "npx"
    assert catalog.servers["http_srv"].connection_type == "streamable-http"
    assert catalog.servers["sse_srv"].connection_type == "sse"
    assert catalog.servers["local"].can_connect is True
    assert catalog.servers["http_srv"].can_connect is True


def test_legacy_aliases_normalize_equivalently(workspace: Path):
    _write_config(
        workspace,
        {
            "mcp_servers": {
                "bing": {
                    "baseUrl": "https://example.com/mcp",
                    "type": "streamable-http",
                    "isActive": True,
                    "timeout": 40,
                    "connect_timeout": 15,
                    "headers": {"X-Test": "1"},
                }
            }
        },
    )
    legacy = load_mcp_catalog(workspace)
    assert legacy.source == "mcp_servers"
    assert legacy.servers["bing"].url == "https://example.com/mcp"
    assert legacy.servers["bing"].enabled is True
    assert legacy.servers["bing"].timeouts_call == 40.0
    assert legacy.servers["bing"].timeouts_connect == 15.0
    assert legacy.servers["bing"].connection_type == "streamable-http"

    _write_config(
        workspace,
        {
            "mcp": {
                "version": 1,
                "servers": {
                    "bing": {
                        "display_name": "bing",
                        "enabled": True,
                        "connection": {
                            "type": "streamable-http",
                            "url": "https://example.com/mcp",
                            "headers": {"X-Test": "1"},
                        },
                        "timeouts": {"call": 40, "connect": 15},
                    }
                },
            }
        },
    )
    canonical = load_mcp_catalog(workspace)
    leg = legacy.servers["bing"].to_runtime_config()
    can = canonical.servers["bing"].to_runtime_config()
    assert leg.transport == can.transport
    assert leg.url == can.url
    assert leg.timeout == can.timeout
    assert leg.connect_timeout == can.connect_timeout
    assert leg.enabled == can.enabled


def test_dual_format_prefers_mcp_and_warns(workspace: Path):
    _write_config(
        workspace,
        {
            "mcp": {
                "version": 1,
                "servers": {
                    "a": {
                        "enabled": True,
                        "connection": {"type": "stdio", "command": "echo"},
                    }
                },
            },
            "mcp_servers": {
                "b": {"url": "https://legacy.example/mcp"},
            },
        },
    )
    catalog = load_mcp_catalog(workspace)
    assert catalog.legacy_shadowed is True
    assert "legacy_shadowed" in catalog.warnings
    assert "a" in catalog.servers
    assert "b" not in catalog.servers


def test_invalid_entry_kept_while_valid_connectable(workspace: Path):
    _write_config(
        workspace,
        {
            "mcp": {
                "version": 1,
                "servers": {
                    "bad": {
                        "enabled": True,
                        "connection": {"type": "stdio"},
                    },
                    "good": {
                        "enabled": True,
                        "connection": {
                            "type": "streamable-http",
                            "url": "https://example.com/mcp",
                        },
                    },
                },
            }
        },
    )
    catalog = load_mcp_catalog(workspace)
    assert catalog.servers["bad"].can_connect is False
    assert catalog.servers["bad"].validation_errors
    assert catalog.servers["good"].can_connect is True
    runtime = load_mcp_server_configs(workspace)
    assert "bad" not in runtime
    assert "good" in runtime


def test_revision_increments_only_on_mutation(workspace: Path):
    _write_config(workspace, {"mcp": {"version": 1, "revision": 0, "servers": {}}})
    c1 = load_mcp_catalog(workspace)
    c2 = load_mcp_catalog(workspace)
    assert c1.revision == c2.revision == 0

    save_mcp_server(
        "demo",
        {
            "display_name": "Demo",
            "enabled": True,
            "connection": {
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "headers": {},
                "command": None,
                "args": [],
                "env": {},
            },
            "auth": {"required_env": []},
            "timeouts": {"connect": 60, "call": 25},
            "tools": {"include": [], "exclude": [], "resources": False, "prompts": False},
            "runtime": {"supports_parallel_tool_calls": False},
        },
        workspace=workspace,
    )
    after_save = load_mcp_catalog(workspace)
    assert after_save.revision == 1

    from app.tools.mcp.config import set_mcp_server_enabled, delete_mcp_server

    after_disable = set_mcp_server_enabled("demo", False, workspace=workspace)
    assert after_disable.revision == 2
    after_delete = delete_mcp_server("demo", workspace=workspace)
    assert after_delete.revision == 3


def test_merge_secret_maps_preserve_replace_clear():
    existing = {"Authorization": "Bearer real", "X-Extra": "keep"}
    preserved = merge_secret_maps(existing, {"Authorization": MASK_LITERAL, "X-Extra": "keep"})
    assert preserved["Authorization"] == "Bearer real"

    replaced = merge_secret_maps(existing, {"Authorization": "Bearer ${NEW_TOKEN}"})
    assert replaced["Authorization"] == "Bearer ${NEW_TOKEN}"

    cleared = merge_secret_maps(existing, {}, cleared_keys=["Authorization"])
    assert "Authorization" not in cleared
    assert cleared["X-Extra"] == "keep"


def test_unknown_non_sensitive_fields_preserved(workspace: Path):
    _write_config(
        workspace,
        {
            "mcp": {
                "version": 1,
                "servers": {
                    "x": {
                        "enabled": True,
                        "connection": {"type": "stdio", "command": "npx"},
                        "custom_note": "keep-me",
                    }
                },
            }
        },
    )
    catalog = load_mcp_catalog(workspace)
    assert catalog.servers["x"].unknown_fields.get("custom_note") == "keep-me"
