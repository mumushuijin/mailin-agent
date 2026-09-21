from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.context.bootstrap import load_bootstrap
from app.context.skills import load_skills_catalog, skills_dir
from app.context.tool_cache import save_tool_result
from app.core.exceptions import AppError
from app.core.settings import Settings, init_workspace
from app.main import app
from app.storage.workspace import (
    SessionStore,
    MemoryStore,
    validate_project_workspace,
)
from app.storage.workspace import longterm_memory_path
from app.tools.packages.filesystem import handlers as fs
from app.tools.packages.shell.executor import resolve_workdir
from app.tools.runtime import set_tool_project


@pytest.fixture
def spaces(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    defaults = Path(__file__).resolve().parents[1] / "workspace_defaults"
    config_defaults = Path(__file__).resolve().parents[1] / "app" / "config" / "defaults"
    if not config_defaults.exists():
        config_defaults = Path(__file__).resolve().parents[1] / "workspace_defaults"
    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    other = tmp_path / "other"
    home.mkdir()
    project.mkdir()
    other.mkdir()
    settings = Settings(
        workspace_path=home,
        workspace_defaults_path=defaults,
        config_dir=config_dir,
        config_defaults_path=config_defaults,
    )
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.storage.project.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.session_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)
    return {"home": home, "defaults": defaults, "project": project, "other": other, "settings": settings}


def test_init_workspace_seeds_agent_home_only(spaces):
    init_workspace(spaces["settings"])
    home: Path = spaces["home"]
    assert spaces["settings"].config_path.exists()
    assert not (home / "CONFIG.json").exists()
    assert (home / "bootstraps" / "SOUL.md").exists()
    assert (home / "memory").is_dir()
    assert (home / "sessions").is_dir()
    assert (home / "skills").is_dir()
    assert not (home / "bootstraps" / "IDENTITY.md").exists()
    assert not (home / "bootstraps" / "BOOTSTRAP.md").exists()
    assert not (home / "sessions").joinpath("..", "project").exists()


def test_init_does_not_create_project_runtime_dirs(spaces):
    init_workspace(spaces["settings"])
    project: Path = spaces["project"]
    validate_project_workspace(project)
    assert not (project / "sessions").exists()
    assert not (project / "memory").exists()
    assert not (project / "skills").exists()


def test_bind_rejects_missing_file_and_agent_spaces(spaces):
    init_workspace(spaces["settings"])
    with pytest.raises(AppError):
        validate_project_workspace(spaces["home"] / "missing")
    file_path = spaces["project"] / "only.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(AppError):
        validate_project_workspace(file_path)
    with pytest.raises(AppError):
        validate_project_workspace(spaces["home"])
    with pytest.raises(AppError):
        validate_project_workspace(spaces["defaults"])
    with pytest.raises(AppError):
        validate_project_workspace(spaces["home"] / "memory")


def test_session_create_list_get_roundtrip(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = store.create(str(spaces["project"]))
    loaded = store.get(sid)
    assert Path(loaded["workspace_path"]) == spaces["project"].resolve()
    listed = store.list()
    assert listed[0]["id"] == sid
    assert Path(listed[0]["workspace_path"]) == spaces["project"].resolve()
    assert not (spaces["project"] / "sessions").exists()


def test_session_rebind(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = store.create(str(spaces["project"]))
    store.rebind(sid, str(spaces["other"]))
    assert Path(store.get(sid)["workspace_path"]) == spaces["other"].resolve()


def test_legacy_session_chat_fails_closed(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = "legacy-unbound"
    store._save({sid: {"created_at": 1, "updated_at": 1}})
    assert store.get(sid).get("workspace_path") is None

    from app.storage.project import require_bound_session

    with pytest.raises(AppError):
        require_bound_session(sid)
    with pytest.raises(AppError):
        require_bound_session(None)


def test_bootstrap_profile_plus_project_agents(spaces):
    init_workspace(spaces["settings"])
    (spaces["project"] / "AGENTS.md").write_text("项目规则：只用 pytest", encoding="utf-8")
    leftover = spaces["home"] / "bootstraps" / "IDENTITY.md"
    leftover.write_text("不该注入的身份", encoding="utf-8")
    (spaces["home"] / "bootstraps" / "BOOTSTRAP.md").write_text("不该注入的引导", encoding="utf-8")

    text, _ = load_bootstrap(spaces["home"], spaces["project"])
    assert "麦林" in text
    assert "项目规则：只用 pytest" in text
    assert "不该注入的身份" not in text
    assert "不该注入的引导" not in text

    empty = spaces["other"]
    text2, _ = load_bootstrap(spaces["home"], empty)
    assert "麦林" in text2
    assert "项目规则：只用 pytest" not in text2


def test_config_stays_in_agent_home(spaces):
    init_workspace(spaces["settings"])
    from app.context.budget import load_context_config
    from app.tools.mcp.config import load_mcp_server_configs

    (spaces["project"] / "CONFIG.json").write_text('{"agent":{"model":"project-model"}}', encoding="utf-8")
    (spaces["other"] / "CONFIG.json").write_text('{"agent":{"model":"other-model"}}', encoding="utf-8")
    cfg_a = load_context_config(spaces["home"])
    cfg_b = load_context_config(spaces["home"])
    assert cfg_a == cfg_b
    assert load_mcp_server_configs(spaces["home"]) == load_mcp_server_configs(spaces["home"])


def test_file_and_shell_sandbox_to_project(spaces):
    init_workspace(spaces["settings"])
    set_tool_project(spaces["project"])
    (spaces["project"] / "hello.txt").write_text("from-project", encoding="utf-8")
    (spaces["home"] / "secret.txt").write_text("from-home", encoding="utf-8")

    assert "from-project" in fs.read_file("hello.txt")
    denied = fs.read_file("../agent_home/secret.txt")
    assert "越界" in denied or "不存在" in denied
    assert "from-home" not in denied

    cwd, err = resolve_workdir(".")
    assert err is None
    assert cwd == spaces["project"].resolve()
    cwd2, err2 = resolve_workdir("../agent_home")
    assert cwd2 is None
    assert err2 is not None


def test_list_directory_keeps_project_across_timeout_thread(spaces):
    init_workspace(spaces["settings"])
    set_tool_project(spaces["project"])
    (spaces["project"] / "hello.py").write_text("print(1)\n", encoding="utf-8")
    from app.resilience import execute_sync
    from app.resilience.policy import ResiliencePolicy

    policy = ResiliencePolicy(
        dependency_id="test.list_directory.ctx",
        timeout_seconds=2.0,
        max_retries=0,
    )
    outcome = execute_sync("test.list_directory.ctx", lambda: fs.list_directory("."), policy=policy)
    assert outcome.ok is True
    assert "hello.py" in (outcome.value or "")
    assert "未绑定项目工作区" not in (outcome.value or "")


def test_large_tool_result_lands_in_project_sidecar(spaces):
    init_workspace(spaces["settings"])
    set_tool_project(spaces["project"])
    path = save_tool_result("sess-x", "call-1", "huge-result")
    rel = path.as_posix()
    assert ".mailin/tool_results/sess-x" in rel
    assert spaces["home"].as_posix() not in rel
    assert not (spaces["project"] / "sessions").exists()
    assert not (spaces["project"] / "memory").exists()
    assert not (spaces["project"] / "skills").exists()


def test_skills_stay_global(spaces):
    init_workspace(spaces["settings"])
    skill_root = skills_dir(spaces["home"])
    (skill_root / "demo").mkdir()
    (skill_root / "demo" / "SKILL.md").write_text("# 演示技能\n", encoding="utf-8")
    catalog, _ = load_skills_catalog(spaces["home"])
    assert "demo" in catalog
    assert not (spaces["project"] / "skills").exists()
    catalog2, _ = load_skills_catalog(spaces["home"])
    assert "demo" in catalog2


def test_memory_stays_in_agent_home(spaces):
    init_workspace(spaces["settings"])
    store = MemoryStore(spaces["home"])
    store.append_daily("用户喜欢深色主题")
    notes = store.list_entries()
    assert notes
    assert "深色主题" in notes[0]["content"]
    assert not any((spaces["project"] / "memory").glob("*"))
    md = longterm_memory_path(spaces["home"])
    assert md.parent == spaces["home"] / "bootstraps"


def test_session_api_create_and_rebind(spaces, monkeypatch):
    init_workspace(spaces["settings"])
    monkeypatch.setattr(
        "app.services.session_service.get_settings",
        lambda: spaces["settings"],
    )
    from app.services.session_service import SessionService

    service = SessionService()
    service.store = SessionStore(spaces["home"])
    sid = service.create(str(spaces["project"]))
    got = service.get(sid)
    assert Path(got.workspace_path) == spaces["project"].resolve()
    rebound = service.rebind(sid, str(spaces["other"]))
    assert Path(rebound.workspace_path) == spaces["other"].resolve()


@pytest.fixture
def client(spaces, monkeypatch):
    init_workspace(spaces["settings"])
    from app.services.session_service import SessionService
    from app.api import session as session_api
    from app.api import chat as chat_api

    svc = SessionService()
    svc.store = SessionStore(spaces["home"])
    monkeypatch.setattr(session_api, "session_service", svc)
    monkeypatch.setattr(chat_api.chat_service, "session_store", svc.store)
    monkeypatch.setattr(chat_api.chat_service, "settings", spaces["settings"])
    with TestClient(app) as test_client:
        yield test_client, spaces, svc


def test_http_session_create_requires_folder(client):
    test_client, spaces, _svc = client
    missing = test_client.post("/api/session/create")
    assert missing.status_code == 422

    rejected = test_client.post("/api/session/create", json={"workspace_path": str(spaces["home"])})
    assert rejected.status_code == 400

    ok = test_client.post("/api/session/create", json={"workspace_path": str(spaces["project"])})
    assert ok.status_code == 200
    sid = ok.json()["session_id"]
    got = test_client.get(f"/api/session/{sid}")
    assert got.status_code == 200
    assert Path(got.json()["workspace_path"]) == spaces["project"].resolve()

    rebound = test_client.post(
        f"/api/session/{sid}/workspace",
        json={"workspace_path": str(spaces["other"])},
    )
    assert rebound.status_code == 200
    assert Path(rebound.json()["workspace_path"]) == spaces["other"].resolve()


def test_http_unbound_chat_fails_closed(client):
    test_client, spaces, svc = client
    sid = "old"
    svc.store._save({sid: {"created_at": 1, "updated_at": 1}})
    r = test_client.post("/api/chat/send", json={"message": "hi", "session_id": sid})
    assert r.status_code == 400
    assert "绑定" in r.json()["detail"] or "文件夹" in r.json()["detail"]
