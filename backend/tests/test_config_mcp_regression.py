"""配置/MCP 兼容与失效回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.config import router as config_router
from app.api.mcp import router as mcp_router
from app.config.persistence import get_revision_tracker
from app.core.exceptions import AppError
from app.core import settings as settings_module
from app.main import app_error_handler
from app.services.mcp_service import McpService, _structured_mcp_errors
from app.tools.registry import clear_tools_cache, load_full_config
from tests.config_test_utils import fake_settings, read_config, write_config


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "CONFIG.json").write_text("{}", encoding="utf-8")
    seed = {
        "agent": {"model": "legacy", "temperature": 0.1, "max_steps": 3},
        "tools": {"enforcement_mode": "enforce", "memory": True, "mcp": True},
        "mcp_servers": {
            "legacy_ok": {
                "enabled": True,
                "type": "sse",
                "url": "https://example.com/sse",
            },
            "legacy_bad": {
                "enabled": True,
                "type": "stdio",
            },
        },
    }
    fake = fake_settings(tmp_path, defaults=defaults)
    write_config(fake, seed)
    monkeypatch.setattr("app.core.settings.get_settings", lambda: fake)
    monkeypatch.setattr("app.services.config_service.get_settings", lambda: fake)
    monkeypatch.setattr("app.tools.mcp.config.get_settings", lambda: fake)
    get_revision_tracker().reset()
    clear_tools_cache()
    return tmp_path


@pytest.fixture
def api(workspace: Path, monkeypatch):
    monkeypatch.setattr("app.services.mcp_service.McpService._invalidate_runtime_caches", lambda self, **kw: None)
    monkeypatch.setattr("app.services.mcp_service.McpService._close_server_runtime", lambda self, sid: None)
    monkeypatch.setattr(
        "app.services.mcp_service.McpService._status_snapshot",
        lambda self: type(
            "Snap",
            (),
            {
                "statuses": [],
                "captured_at": 0.0,
                "source": "test",
                "stale": False,
                "config_revision": 0,
                "applied_revision": 0,
            },
        )(),
    )
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(config_router, prefix="/api/config")
    app.include_router(mcp_router, prefix="/api/mcp")
    return TestClient(app)


def test_legacy_and_canonical_and_invalid_isolation(workspace: Path):
    effective = load_full_config()
    assert effective["agent"]["model"] == "legacy"
    from app.tools.mcp.config import load_mcp_catalog

    catalog = load_mcp_catalog(workspace)
    assert "legacy_ok" in catalog.servers
    assert "legacy_bad" in catalog.servers
    assert catalog.servers["legacy_bad"].validation_errors
    assert catalog.servers["legacy_ok"].enabled


def test_dual_mcp_source_prefers_canonical(workspace: Path):
    settings = settings_module.get_settings()
    raw = read_config(settings)
    raw["mcp"] = {
        "version": 1,
        "revision": 2,
        "servers": {
            "canonical": {
                "enabled": True,
                "connection": {"type": "sse", "url": "https://example.com/canonical"},
            }
        },
    }
    write_config(settings, raw)
    from app.tools.mcp.config import load_mcp_catalog

    catalog = load_mcp_catalog(workspace)
    assert "canonical" in catalog.servers
    assert "legacy_ok" not in catalog.servers
    assert catalog.legacy_shadowed or catalog.source == "mcp"


def test_structured_mcp_validation_errors(api):
    res = api.post(
        "/api/mcp/validate",
        json={
            "id": "bad",
            "enabled": True,
            "connection": {"type": "stdio", "command": None, "args": [], "env": {}, "headers": {}},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["validation_errors"]
    assert body["errors"]
    assert any(e["path"].startswith("/connection") for e in body["errors"])


def test_tools_module_change_does_not_require_mcp_reload(api, workspace: Path, monkeypatch):
    mcp_invalidate = {"n": 0}

    def count_invalidate(self, **kwargs):
        mcp_invalidate["n"] += 1

    monkeypatch.setattr(McpService, "_invalidate_runtime_caches", count_invalidate)
    monkeypatch.setattr("app.services.config_service.get_chat_model.cache_clear", lambda: None)
    monkeypatch.setattr("app.services.config_service.get_graph.cache_clear", lambda: None)
    cleared = {"n": 0}
    monkeypatch.setattr(
        "app.services.config_service.clear_tools_cache",
        lambda: cleared.__setitem__("n", cleared["n"] + 1),
    )

    revision = api.get("/api/config/modules").json()["revision"]
    res = api.put(
        "/api/config/modules/tools",
        json={
            "base_revision": revision,
            "value": {"enforcement_mode": "audit", "memory": False},
        },
    )
    assert res.status_code == 200, res.text
    assert cleared["n"] >= 1
    assert mcp_invalidate["n"] == 0
    effective = load_full_config()
    assert effective["tools"]["enforcement_mode"] == "audit"


def test_mcp_save_bumps_revision_and_clears_tool_registry(api, workspace: Path, monkeypatch):
    calls = {"clear": 0, "stale": 0}

    def fake_clear():
        calls["clear"] += 1

    monkeypatch.setattr("app.tools.registry.clear_tools_cache", fake_clear)
    monkeypatch.setattr(
        "app.services.mcp_service.McpService._invalidate_runtime_caches",
        lambda self, **kw: (
            calls.__setitem__("stale", calls["stale"] + 1),
            fake_clear(),
        ),
    )

    payload = {
        "display_name": "New",
        "enabled": True,
        "connection": {
            "type": "streamable-http",
            "url": "https://example.com/mcp",
            "headers": {},
            "command": None,
            "args": [],
            "env": {},
        },
    }
    before = read_config(settings_module.get_settings())
    res = api.put("/api/mcp/newsrv", json=payload)
    assert res.status_code == 200, res.text
    after = read_config(settings_module.get_settings())
    assert "mcp" in after
    assert "newsrv" in after["mcp"]["servers"]
    assert after["mcp"]["revision"] >= before.get("mcp", {}).get("revision", 0)
    assert calls["stale"] >= 1


def test_structured_helper_maps_paths():
    errors = _structured_mcp_errors(["缺少 stdio command", "缺少 HTTP url"])
    assert errors[0].path == "/connection/command"
    assert errors[1].path == "/connection/url"
