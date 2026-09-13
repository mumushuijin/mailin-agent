from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import AppError
from app.core.settings import Settings, init_workspace
from app.main import app
from app.schemas.session import (
    PLACEHOLDER_TITLE,
    TITLE_SOURCE_AUTO,
    TITLE_SOURCE_PLACEHOLDER,
    TITLE_SOURCE_USER,
)
from app.storage.workspace import TITLE_MAX_LEN, SessionStore, derive_session_title
from app.services.session_service import SessionService


@pytest.fixture
def spaces(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    defaults = Path(__file__).resolve().parents[1] / "workspace_defaults"
    project = tmp_path / "project"
    other = tmp_path / "other"
    home.mkdir()
    project.mkdir()
    other.mkdir()
    settings = Settings(
        workspace_path=home,
        workspace_defaults_path=defaults,
    )
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.storage.project.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.session_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)
    return {"home": home, "defaults": defaults, "project": project, "other": other, "settings": settings}


def test_derive_session_title_uses_first_line_and_truncates():
    assert derive_session_title("  第一行  \n第二行") == "第一行"
    assert derive_session_title("") == PLACEHOLDER_TITLE
    long = "字" * 50
    assert derive_session_title(long) == "字" * TITLE_MAX_LEN
    assert derive_session_title(long) != long
    prefixed = "请帮我" + "甲" * 20
    assert derive_session_title(prefixed) == "甲" * TITLE_MAX_LEN
    assert derive_session_title("请问优化侧栏") == "优化侧栏"


def test_create_list_get_include_placeholder_title(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = store.create(str(spaces["project"]))
    loaded = store.get(sid)
    assert loaded["id"] == sid
    assert loaded["title"] == PLACEHOLDER_TITLE
    assert loaded["title_source"] == TITLE_SOURCE_PLACEHOLDER
    assert Path(loaded["workspace_path"]) == spaces["project"].resolve()
    assert "created_at" in loaded and "updated_at" in loaded
    listed = store.list()
    assert listed[0]["id"] == sid
    assert listed[0]["title"] == PLACEHOLDER_TITLE
    assert listed[0]["title_source"] == TITLE_SOURCE_PLACEHOLDER
    assert Path(listed[0]["workspace_path"]) == spaces["project"].resolve()


def test_legacy_session_missing_title_is_hydrated(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = "legacy-no-title"
    store._save(
        {
            sid: {
                "created_at": 1,
                "updated_at": 1,
                "workspace_path": str(spaces["project"]),
            }
        }
    )
    assert store.get(sid)["title"] == PLACEHOLDER_TITLE
    assert store.get(sid)["title_source"] == TITLE_SOURCE_PLACEHOLDER
    assert store.list()[0]["title"] == PLACEHOLDER_TITLE
    rebound = store.rebind(sid, str(spaces["other"]))
    assert rebound["title"] == PLACEHOLDER_TITLE
    assert rebound["title_source"] == TITLE_SOURCE_PLACEHOLDER


def test_legacy_custom_title_is_hydrated_as_auto(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = "legacy-custom-title"
    store._save(
        {
            sid: {
                "created_at": 1,
                "updated_at": 1,
                "workspace_path": str(spaces["project"]),
                "title": "旧自动标题",
            }
        }
    )
    loaded = store.get(sid)
    assert loaded["title"] == "旧自动标题"
    assert loaded["title_source"] == TITLE_SOURCE_AUTO


def test_rename_rejects_blank_and_keeps_binding(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = store.create(str(spaces["project"]))
    with pytest.raises(AppError, match="标题不能为空"):
        store.rename(sid, "   ")
    assert store.get(sid)["title"] == PLACEHOLDER_TITLE
    renamed = store.rename(sid, "  侧栏优化  ")
    assert renamed["title"] == "侧栏优化"
    assert renamed["title_source"] == TITLE_SOURCE_USER
    assert Path(renamed["workspace_path"]) == spaces["project"].resolve()


def test_first_message_sets_title_second_does_not_overwrite(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = store.create(str(spaces["project"]))
    store.maybe_set_title_from_message(sid, "优化左侧栏\n补充说明")
    loaded = store.get(sid)
    assert loaded["title"] == "优化左侧栏"
    assert loaded["title_source"] == TITLE_SOURCE_AUTO
    store.maybe_set_title_from_message(sid, "第二条消息")
    assert store.get(sid)["title"] == "优化左侧栏"
    assert store.get(sid)["title_source"] == TITLE_SOURCE_AUTO


def test_renamed_title_is_not_overwritten_by_message(spaces):
    init_workspace(spaces["settings"])
    store = SessionStore(spaces["home"])
    sid = store.create(str(spaces["project"]))
    store.rename(sid, "自定义标题")
    store.maybe_set_title_from_message(sid, "用户第一句话")
    assert store.get(sid)["title"] == "自定义标题"
    assert store.get(sid)["title_source"] == TITLE_SOURCE_USER
    store.apply_auto_title(sid, "不应覆盖")
    assert store.get(sid)["title"] == "自定义标题"


def test_delete_session_leaves_project_files(spaces):
    init_workspace(spaces["settings"])
    marker = spaces["project"] / "user-note.txt"
    marker.write_text("keep me", encoding="utf-8")
    store = SessionStore(spaces["home"])
    sid = store.create(str(spaces["project"]))
    store.delete(sid)
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert spaces["project"].is_dir()
    assert all(item["id"] != sid for item in store.list())


@pytest.fixture
def title_client(spaces, monkeypatch):
    init_workspace(spaces["settings"])
    from app.api import session as session_api

    svc = SessionService()
    svc.store = SessionStore(spaces["home"])
    monkeypatch.setattr(session_api, "session_service", svc)
    with TestClient(app) as test_client:
        yield test_client, spaces, svc


def test_http_list_includes_title_fields(title_client):
    test_client, spaces, _svc = title_client
    created = test_client.post(
        "/api/session/create",
        json={"workspace_path": str(spaces["project"])},
    )
    assert created.status_code == 200
    sid = created.json()["session_id"]
    listed = test_client.get("/api/session/list")
    assert listed.status_code == 200
    item = next(s for s in listed.json()["sessions"] if s["id"] == sid)
    assert item["title"] == PLACEHOLDER_TITLE
    assert item["title_source"] == TITLE_SOURCE_PLACEHOLDER
    assert Path(item["workspace_path"]) == spaces["project"].resolve()
    assert "created_at" in item and "updated_at" in item
    got = test_client.get(f"/api/session/{sid}")
    assert got.json()["title"] == PLACEHOLDER_TITLE
    assert got.json()["title_source"] == TITLE_SOURCE_PLACEHOLDER


def test_http_delete_leaves_project_files(title_client):
    test_client, spaces, svc = title_client
    marker = spaces["project"] / "keep.txt"
    marker.write_text("still here", encoding="utf-8")
    sid = svc.create(str(spaces["project"]))
    deleted = test_client.delete(f"/api/session/{sid}")
    assert deleted.status_code == 200
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "still here"
    listed = test_client.get("/api/session/list").json()["sessions"]
    assert all(item["id"] != sid for item in listed)


def test_http_rename_success_and_blank_rejected(title_client):
    test_client, spaces, svc = title_client
    sid = svc.create(str(spaces["project"]))
    ok = test_client.patch(f"/api/session/{sid}/title", json={"title": "项目导航"})
    assert ok.status_code == 200
    assert ok.json()["title"] == "项目导航"
    assert ok.json()["title_source"] == TITLE_SOURCE_USER
    assert Path(ok.json()["workspace_path"]) == spaces["project"].resolve()
    listed = test_client.get("/api/session/list").json()["sessions"]
    assert next(s for s in listed if s["id"] == sid)["title"] == "项目导航"

    blank = test_client.patch(f"/api/session/{sid}/title", json={"title": "  "})
    assert blank.status_code == 400
    assert test_client.get(f"/api/session/{sid}").json()["title"] == "项目导航"


@pytest.mark.asyncio
async def test_chat_service_sets_title_on_first_accepted_message(spaces, monkeypatch):
    init_workspace(spaces["settings"])
    spaces["settings"].openai_api_key = "test-key"
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: spaces["settings"])
    monkeypatch.setattr("app.core.settings.get_settings", lambda: spaces["settings"])

    async def noop_memory(_message):
        return None

    class DummyGraph:
        async def ainvoke(self, _state, _config):
            return {"messages": []}

    monkeypatch.setattr("app.services.chat_service.prepare_session_memory", noop_memory)
    monkeypatch.setattr("app.services.chat_service.get_graph", lambda: DummyGraph())
    monkeypatch.setattr("app.services.chat_service.dispatch_post_llm_call", lambda **_kwargs: None)
    monkeypatch.setattr("app.services.chat_service.dispatch_observe", lambda *_args, **_kwargs: None)

    from app.services.chat_service import ChatService

    chat = ChatService()
    chat.settings = spaces["settings"]
    chat.session_store = SessionStore(spaces["home"])
    sid = chat.session_store.create(str(spaces["project"]))

    await chat.send_sync("请优化侧栏\n细节", sid)
    first = chat.session_store.get(sid)
    assert first["title"] == "请优化侧栏"
    assert first["title_source"] == TITLE_SOURCE_AUTO
    await chat.send_sync("第二条不应覆盖", sid)
    assert chat.session_store.get(sid)["title"] == "请优化侧栏"
    assert chat.session_store.get(sid)["title_source"] == TITLE_SOURCE_AUTO


@pytest.mark.asyncio
async def test_chat_service_refines_auto_title_after_done(spaces, monkeypatch):
    init_workspace(spaces["settings"])
    spaces["settings"].openai_api_key = "test-key"
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: spaces["settings"])
    monkeypatch.setattr("app.core.settings.get_settings", lambda: spaces["settings"])

    async def noop_memory(_message):
        return None

    class DummyGraph:
        async def ainvoke(self, _state, _config):
            return {"messages": []}

    monkeypatch.setattr("app.services.chat_service.prepare_session_memory", noop_memory)
    monkeypatch.setattr("app.services.chat_service.get_graph", lambda: DummyGraph())
    monkeypatch.setattr("app.services.chat_service.dispatch_post_llm_call", lambda **_kwargs: None)
    monkeypatch.setattr("app.services.chat_service.dispatch_observe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.services.chat_service.extract_final_content",
        lambda _messages: "已完成",
    )

    from app.services.chat_service import ChatService

    chat = ChatService()
    chat.settings = spaces["settings"]
    chat.session_store = SessionStore(spaces["home"])
    sid = chat.session_store.create(str(spaces["project"]))

    async def fake_title(_user, _assistant):
        return "侧栏短标题"

    chat._llm_short_title = fake_title  # type: ignore[method-assign]
    await chat.send_sync("请帮我优化左侧栏交互", sid)
    loaded = chat.session_store.get(sid)
    assert loaded["title"] == "侧栏短标题"
    assert loaded["title_source"] == TITLE_SOURCE_AUTO

    chat.session_store.rename(sid, "我起的名字")
    await chat.send_sync("再发一条", sid)
    assert chat.session_store.get(sid)["title"] == "我起的名字"
    assert chat.session_store.get(sid)["title_source"] == TITLE_SOURCE_USER
