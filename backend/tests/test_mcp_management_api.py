"""MCP 管理 API / service 集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mcp import router as mcp_router
from app.core import settings as settings_module
from app.tools.mcp.bridge import get_mcp_cards, set_mcp_cards
from app.tools.card import make_card


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    from tests.config_test_utils import fake_settings, write_config

    fake = fake_settings(tmp_path, defaults=tmp_path)
    write_config(fake, {"mcp": {"version": 1, "revision": 0, "servers": {}}})

    monkeypatch.setattr("app.core.settings.get_settings", lambda: fake)
    monkeypatch.setattr("app.tools.mcp.config.get_settings", lambda: fake)
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
    return tmp_path


@pytest.fixture
def api(workspace: Path):
    app = FastAPI()
    app.include_router(mcp_router, prefix="/api/mcp")
    return TestClient(app)


def test_mcp_crud_and_revision(api, workspace: Path):
    payload = {
        "display_name": "Demo",
        "enabled": True,
        "connection": {
            "type": "streamable-http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer ${TOKEN}"},
            "command": None,
            "args": [],
            "env": {},
        },
        "auth": {"required_env": []},
        "timeouts": {"connect": 30, "call": 20},
        "tools": {"include": [], "exclude": [], "resources": False, "prompts": False},
        "runtime": {"supports_parallel_tool_calls": False},
    }
    res = api.put("/api/mcp/demo", json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["config_revision"] == 1
    assert body["server"]["id"] == "demo"
    assert body["server"]["connection"]["headers"]["Authorization"] == "Bearer ${TOKEN}"

    listed = api.get("/api/mcp/list")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    detail = api.get("/api/mcp/demo")
    assert detail.status_code == 200
    dumped = json.dumps(detail.json())
    assert "real-secret" not in dumped
    assert detail.json()["connection"]["headers"]["Authorization"] == "Bearer ${TOKEN}"

    disabled = api.post("/api/mcp/demo/disable")
    assert disabled.status_code == 200
    assert disabled.json()["server"]["enabled"] is False
    assert disabled.json()["config_revision"] == 2

    deleted = api.delete("/api/mcp/demo")
    assert deleted.status_code == 200
    assert deleted.json()["config_revision"] == 3
    assert api.get("/api/mcp/demo").status_code == 404


def test_validate_does_not_write(api, workspace: Path):
    before = settings_module.get_settings().config_path.read_text(encoding="utf-8")
    res = api.post(
        "/api/mcp/validate",
        json={
            "id": "draft",
            "display_name": "Draft",
            "enabled": True,
            "connection": {
                "type": "stdio",
                "command": "npx",
                "args": [],
                "env": {},
                "headers": {},
            },
        },
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    after = settings_module.get_settings().config_path.read_text(encoding="utf-8")
    assert before == after


def test_test_connection_does_not_replace_cards(api, monkeypatch):
    existing = [
        make_card(
            package="mcp-existing",
            name="mcp_existing_tool",
            handler=lambda: "ok",
            summary="x",
            description="x",
            display_name="Existing",
            display_icon="🔌",
        )
    ]
    set_mcp_cards(existing)
    monkeypatch.setattr(
        "app.tools.mcp.lifecycle.test_mcp_server_connection",
        lambda config: {
            "ok": True,
            "state": "ready",
            "tool_count": 2,
            "tool_names": ["a", "b"],
            "error": None,
            "error_code": None,
        },
    )
    res = api.post(
        "/api/mcp/test",
        json={
            "id": "tmp",
            "enabled": True,
            "connection": {
                "type": "streamable-http",
                "url": "https://example.com/mcp",
            },
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["replaced_runtime"] is False
    assert [c.name for c in get_mcp_cards()] == ["mcp_existing_tool"]
    set_mcp_cards([])


def test_masked_save_preserves_secret(api, workspace: Path):
    create = {
        "display_name": "Sec",
        "enabled": True,
        "connection": {
            "type": "streamable-http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer real-token-value"},
        },
    }
    assert api.put("/api/mcp/sec", json=create).status_code == 200

    update = {
        "display_name": "Sec Renamed",
        "enabled": True,
        "connection": {
            "type": "streamable-http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "********"},
        },
    }
    res = api.put("/api/mcp/sec", json=update)
    assert res.status_code == 200
    raw = json.loads(settings_module.get_settings().config_path.read_text(encoding="utf-8"))
    assert raw["mcp"]["servers"]["sec"]["connection"]["headers"]["Authorization"] == "Bearer real-token-value"

    clear = {
        "display_name": "Sec Renamed",
        "enabled": True,
        "connection": {
            "type": "streamable-http",
            "url": "https://example.com/mcp",
            "headers": {},
        },
        "clear_headers": ["Authorization"],
    }
    assert api.put("/api/mcp/sec", json=clear).status_code == 200
    raw = json.loads(settings_module.get_settings().config_path.read_text(encoding="utf-8"))
    assert "Authorization" not in raw["mcp"]["servers"]["sec"]["connection"]["headers"]
