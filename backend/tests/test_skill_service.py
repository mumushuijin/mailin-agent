from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.context.skills import load_skills_catalog, skills_dir
from app.core.exceptions import AppError, NotFoundError
from app.core.settings import Settings, init_workspace
from app.main import app
from app.schemas.skill import SkillEntry
from app.services.skill_service import SkillService, clear_skill_cache, load_skill_config
from app.tools.registry import ToolRegistry, clear_tools_cache
from app.tools.tool_search import ToolSearchConfig, assemble_bind_tools


@pytest.fixture(autouse=True)
def _clear_skill_state():
    clear_skill_cache()
    clear_tools_cache()
    yield
    clear_skill_cache()
    clear_tools_cache()


def _write_skill(root: Path, folder: str, content: str, meta: dict | None = None) -> Path:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    if meta is not None:
        (directory / "_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False),
            encoding="utf-8",
        )
    return directory


def test_skill_entry_serialization_is_stable(tmp_path: Path):
    entry = SkillEntry(
        id="backend",
        name="Backend",
        description="Build backend services",
        version="1.0.0",
        source="project",
        path=str(tmp_path / "backend"),
        enabled=True,
        active=True,
        health="ok",
        summary="Build backend services",
    )

    payload = entry.model_dump()

    assert payload["id"] == "backend"
    assert payload["source"] == "project"
    assert payload["health"] == "ok"
    assert payload["shadowed_by"] is None
    assert payload["errors"] == []


def test_skill_config_defaults_are_compatible(tmp_path: Path):
    config = load_skill_config(tmp_path)

    assert config["global_roots"] == ["skills"]
    assert config["project_enabled"] is True
    assert config["catalog_max_items"] > 0
    assert config["disabled"] == []


def test_discovers_global_and_project_skills(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    global_root = skills_dir(home)
    _write_skill(global_root, "research", "# Research\n\nFind sources.")
    _write_skill(project / ".agents" / "skills", "backend", "# Backend\n\n项目后端规范")

    result = SkillService(home, project).list()
    by_id = {(item.id, item.source): item for item in result.skills}

    assert ("research", "global") in by_id
    assert ("backend", "project") in by_id


def test_project_without_skills_still_returns_global(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    _write_skill(skills_dir(home), "research", "# Research\n\nFind sources.")

    result = SkillService(home, project).list()

    assert result.total == 1
    assert result.skills[0].source == "global"


def test_parses_frontmatter_meta_and_body_summary(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    _write_skill(
        skills_dir(home),
        "backend-1.0.0",
        """---
name: Backend
description: Build reliable backend services
tags:
  - api
triggers:
  - backend
metadata:
  version: "1.2.3"
---

# Full backend playbook

SECRET FULL BODY
""",
        meta={"slug": "ignored", "version": "1.0.0"},
    )

    entry = SkillService(home).list().skills[0]

    assert entry.id == "backend"
    assert entry.name == "Backend"
    assert entry.version == "1.2.3"
    assert entry.description == "Build reliable backend services"
    assert "api" in entry.tags
    assert "backend" in entry.triggers


def test_meta_slug_and_body_fallback(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    _write_skill(
        skills_dir(home),
        "frontend-design-pro-1.0.0",
        "# 前端设计\n\n让界面更专业",
        meta={"slug": "frontend-design-pro", "version": "1.0.0"},
    )

    entry = SkillService(home).list().skills[0]

    assert entry.id == "frontend-design-pro"
    assert entry.version == "1.0.0"
    assert entry.summary == "前端设计"


def test_project_skill_overrides_global_duplicate(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    _write_skill(skills_dir(home), "backend", "# Backend\n\nGlobal")
    _write_skill(project / ".agents" / "skills", "backend", "# Backend\n\nProject")

    result = SkillService(home, project).list()
    global_entry = next(item for item in result.skills if item.source == "global")
    project_entry = next(item for item in result.skills if item.source == "project")

    assert project_entry.active is True
    assert global_entry.active is False
    assert global_entry.health == "shadowed"
    assert global_entry.shadowed_by == "project:backend"


def test_disabled_skill_is_not_loadable(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    _write_skill(skills_dir(home), "backend", "# Backend\n\nFull instructions")
    service = SkillService(home)

    disabled = service.set_enabled("backend", False, source="global").skill
    detail = SkillService(home).detail("backend", source="global")

    assert disabled.enabled is False
    assert detail.health == "disabled"
    assert detail.content is None
    assert "skill 已禁用" in detail.errors


def test_broken_skill_does_not_break_catalog(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    _write_skill(skills_dir(home), "ok", "# OK\n\nusable")
    _write_skill(skills_dir(home), "broken", "---\nname: [\n---\nbody")

    result = SkillService(home).list()
    by_id = {item.id: item for item in result.skills}

    assert by_id["ok"].health == "ok"
    assert by_id["broken"].health == "error"
    assert by_id["broken"].errors


def test_resource_access_rejects_traversal_missing_and_cycles(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    directory = _write_skill(
        skills_dir(home),
        "loader",
        "# Loader\n\nRead [A](references/a.md) and [missing](references/missing.md).",
    )
    refs = directory / "references"
    refs.mkdir()
    (refs / "a.md").write_text("A links [B](references/b.md)", encoding="utf-8")
    (refs / "b.md").write_text("B links [A](references/a.md)", encoding="utf-8")
    service = SkillService(home)

    with pytest.raises(AppError):
        service.read_resource("loader", "../outside.md")
    with pytest.raises(NotFoundError):
        service.read_resource("loader", "references/missing.md")
    detail = service.detail("loader", include_references=True)

    assert any(resource.error == "skill 资源不存在" for resource in detail.resources)
    assert any(resource.error == "skill 资源循环引用" for resource in detail.resources)


def test_match_prefers_project_and_command_alias(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    _write_skill(skills_dir(home), "openspec-propose", "# openspec-propose\n\nGlobal")
    _write_skill(project / ".agents" / "skills", "openspec-propose", "# openspec-propose\n\nProject")

    matches = SkillService(home, project).match("/opsx-propose 我想提案").matches

    assert matches
    assert matches[0].skill.id == "openspec-propose"
    assert matches[0].skill.source == "project"
    assert matches[0].reason == "command"


def test_catalog_is_lightweight_and_project_scoped(tmp_path: Path):
    home = tmp_path / "home"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    home.mkdir()
    project_a.mkdir()
    project_b.mkdir()
    _write_skill(skills_dir(home), "global", "# Global\n\nGLOBAL FULL BODY")
    _write_skill(project_a / ".agents" / "skills", "project-only", "# Project Only\n\nPROJECT A FULL BODY")

    catalog_a, _ = load_skills_catalog(home, project_a)
    catalog_b, _ = load_skills_catalog(home, project_b)

    assert "global [global]" in catalog_a
    assert "project-only [project]" in catalog_a
    assert "PROJECT A FULL BODY" not in catalog_a
    assert "project-only" not in catalog_b


def test_refresh_picks_up_filesystem_changes(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    root = project / ".agents" / "skills"
    service = SkillService(home, project)
    assert service.list().total == 0

    _write_skill(root, "new-skill", "# New Skill\n\nFresh")
    cached = service.list()
    refreshed = service.refresh()

    assert cached.total == 0
    assert refreshed.total == 1
    assert refreshed.skills[0].source == "project"


def test_skill_api_and_router(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    defaults = Path(__file__).resolve().parents[1] / "workspace_defaults"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = Settings(workspace_path=home, workspace_defaults_path=defaults)
    init_workspace(settings)
    _write_skill(skills_dir(home), "research", "# Research\n\nGlobal")
    _write_skill(project / ".agents" / "skills", "backend", "# Backend\n\nProject")

    from app.api import skills as skills_api

    monkeypatch.setattr("app.services.skill_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr(skills_api, "_service", lambda project_path=None: SkillService(home, Path(project_path) if project_path else project))

    with TestClient(app) as client:
        listed = client.get("/api/skills/list", params={"source": "project", "project_path": str(project)})
        detail = client.get("/api/skills/backend", params={"source": "project", "project_path": str(project)})
        matches = client.get("/api/skills/matches", params={"query": "backend api", "project_path": str(project)})
        disabled = client.post("/api/skills/backend/disable", params={"source": "project", "project_path": str(project)})
        openapi = client.get("/openapi.json")

    assert listed.status_code == 200
    assert listed.json()["skills"][0]["source"] == "project"
    assert detail.status_code == 200
    assert detail.json()["content"] is not None
    assert matches.status_code == 200
    assert matches.json()["matches"][0]["skill"]["id"] == "backend"
    assert disabled.status_code == 200
    assert disabled.json()["skill"]["enabled"] is False
    assert "/api/skills/list" in openapi.json()["paths"]


def test_skill_tools_are_available_by_default():
    config = {
        "tools": {
            "filesystem": False,
            "shell": False,
            "session": False,
            "skills": True,
            "memory": False,
            "calculator": False,
            "datetime": False,
            "web_search": False,
            "mcp": False,
        }
    }
    cards = ToolRegistry(config=config).resolve_cards()
    names = {tool.name for tool in assemble_bind_tools(cards, ToolSearchConfig.from_config(config)).tools}

    assert {"skill_list", "skill_match", "skill_read"}.issubset(names)
