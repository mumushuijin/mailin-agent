"""配置模块 API 与持久化测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.config import router as config_router
from app.config.persistence import get_revision_tracker
from app.core.exceptions import AppError
from app.main import app_error_handler
from tests.config_test_utils import fake_settings, read_config, write_config


@pytest.fixture
def settings(tmp_path: Path, monkeypatch):
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    seed = {
        "agent": {"model": "default", "temperature": 0.7, "max_steps": 10},
        "tools": {"enforcement_mode": "enforce", "memory": True},
        "middleware": {},
        "skills": {"global_roots": ["skills"], "disabled": []},
        "telemetry": {"langsmith_enabled": False, "sample_rate": 0.01},
        "context": {"max_tokens": 1000},
        "mcp_servers": {},
    }
    (defaults / "CONFIG.json").write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    cfg_defaults = tmp_path / "config_defaults"
    cfg_defaults.mkdir()
    (cfg_defaults / "CONFIG.json").write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    (tmp_path / "bootstraps").mkdir()
    (tmp_path / "bootstraps" / "SOUL.md").write_text("# 麦林\n", encoding="utf-8")

    fake = fake_settings(tmp_path, defaults=defaults, config_defaults=cfg_defaults)
    write_config(fake, seed)

    monkeypatch.setattr("app.core.settings.get_settings", lambda: fake)
    monkeypatch.setattr("app.services.config_service.get_settings", lambda: fake)
    get_revision_tracker().reset()
    return fake


@pytest.fixture
def workspace(settings):
    return settings.workspace_path


@pytest.fixture
def api(settings):
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(config_router, prefix="/api/config")
    return TestClient(app)


def test_schema_structured_error_shape():
    from app.schemas.config import ConfigValidationPayload, ConfigFieldError

    payload = ConfigValidationPayload(
        errors=[ConfigFieldError(path="/max_steps", kind="type", message="必须是数字", expected="number")],
        revision=3,
        module="agent",
    )
    data = payload.model_dump()
    assert data["code"] == "CONFIG_VALIDATION_ERROR"
    assert data["errors"][0]["path"] == "/max_steps"


def test_list_modules_and_markdown_file_api_isolated(api, settings):
    modules = api.get("/api/config/modules")
    assert modules.status_code == 200, modules.text
    body = modules.json()
    keys = [m["key"] for m in body["modules"]]
    assert keys == ["agent", "tools"]
    assert "SOUL" not in keys
    assert "context" not in keys
    assert "CONFIG" not in keys

    soul = api.get("/api/config/SOUL")
    assert soul.status_code == 200
    assert "麦林" in soul.json()["content"]

    before = Path(settings.config_path).read_text(encoding="utf-8")
    put = api.put("/api/config/SOUL", json={"content": "# 麦林\nhello\n"})
    assert put.status_code == 200
    after = Path(settings.config_path).read_text(encoding="utf-8")
    assert before == after


def test_system_module_not_updatable_via_modules_api(api):
    revision = api.get("/api/config/modules").json()["revision"]
    res = api.put(
        "/api/config/modules/context",
        json={"base_revision": revision, "value": {"max_tokens": 1}},
    )
    assert res.status_code == 404


def test_validate_and_update_module(api, settings):
    listed = api.get("/api/config/modules").json()
    revision = listed["revision"]

    bad = api.post(
        "/api/config/modules/agent/validate",
        json={"value": {"model": "x", "temperature": 9, "max_steps": 1}},
    )
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
    assert any(e["path"] == "/temperature" for e in bad.json()["errors"])

    ok = api.put(
        "/api/config/modules/agent",
        json={
            "base_revision": revision,
            "value": {"model": "updated", "temperature": 0.2, "max_steps": 7},
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["revision"] > revision
    assert body["value"]["model"] == "updated"

    raw = read_config(settings)
    assert raw["config"]["agent"]["model"] == "updated"
    assert raw["config"]["tools"]["enforcement_mode"] == "enforce"
    assert Path(settings.workspace_path / "CONFIG.json").exists() is False


def test_revision_conflict(api):
    revision = api.get("/api/config/modules").json()["revision"]
    first = api.put(
        "/api/config/modules/tools",
        json={
            "base_revision": revision,
            "value": {"enforcement_mode": "audit", "memory": True},
        },
    )
    assert first.status_code == 200
    conflict = api.put(
        "/api/config/modules/tools",
        json={
            "base_revision": revision,
            "value": {"enforcement_mode": "enforce", "memory": False},
        },
    )
    assert conflict.status_code == 409
    payload = conflict.json()
    assert payload["code"] == "CONFIG_REVISION_CONFLICT"
    assert "revision" in payload


def test_atomic_write_keeps_old_file_on_failure(settings, monkeypatch):
    from app.services.config_service import ConfigService

    service = ConfigService()
    original = Path(settings.config_path).read_text(encoding="utf-8")
    revision = service.current_revision()

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("app.services.config_service.atomic_write_json", boom)
    with pytest.raises(Exception):
        service.update_module(
            "agent",
            base_revision=revision,
            value={"model": "nope", "temperature": 0.1, "max_steps": 2},
        )
    assert Path(settings.config_path).read_text(encoding="utf-8") == original


def test_ordinary_module_update_invalidates_tools_cache(api, monkeypatch):
    calls = {"clear": 0}

    def fake_clear():
        calls["clear"] += 1

    monkeypatch.setattr("app.services.config_service.clear_tools_cache", fake_clear)
    monkeypatch.setattr("app.services.config_service.get_chat_model.cache_clear", lambda: None)
    monkeypatch.setattr("app.services.config_service.get_graph.cache_clear", lambda: None)

    revision = api.get("/api/config/modules").json()["revision"]
    res = api.put(
        "/api/config/modules/tools",
        json={
            "base_revision": revision,
            "value": {"enforcement_mode": "audit", "memory": False},
        },
    )
    assert res.status_code == 200, res.text
    assert calls["clear"] >= 1


def test_canonical_tools_visible_to_runtime_loader(api, settings, monkeypatch):
    from app.tools.registry import load_full_config

    monkeypatch.setattr("app.services.config_service.get_chat_model.cache_clear", lambda: None)
    monkeypatch.setattr("app.services.config_service.get_graph.cache_clear", lambda: None)
    monkeypatch.setattr("app.services.config_service.clear_tools_cache", lambda: None)

    revision = api.get("/api/config/modules").json()["revision"]
    res = api.put(
        "/api/config/modules/tools",
        json={
            "base_revision": revision,
            "value": {"enforcement_mode": "audit", "memory": False, "web_search": True},
        },
    )
    assert res.status_code == 200, res.text
    effective = load_full_config()
    assert effective["tools"]["enforcement_mode"] == "audit"
    assert effective["tools"]["memory"] is False
    assert effective["tools"]["web_search"] is True
