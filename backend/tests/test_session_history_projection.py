from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.settings import Settings, init_workspace
from app.schemas.session import ChatMessage
from app.services.chat_service import ChatService
from app.storage.history_projection import HistoryProjectionStore, PROJECTION_VERSION
from app.storage.workspace import SessionStore


@pytest.fixture
def history_spaces(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = Settings(
        workspace_path=home,
        workspace_defaults_path=Path(__file__).resolve().parents[1] / "workspace_defaults",
        openai_api_key="test-key",
    )
    init_workspace(settings)
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.storage.project.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)
    return settings, home, project


def test_projection_pages_newest_messages_and_preserves_metadata(history_spaces):
    _settings, home, _project = history_spaces
    store = HistoryProjectionStore(home)
    messages = [
        ChatMessage(role="user", content=f"u{i}", timestamp=i)
        for i in range(5)
    ]
    store.write(
        "sess-1",
        messages,
        context_usage={"total_tokens": 10},
        api_usage={"total_tokens": 3},
        session_token_stats={"request_count": 1},
        todos=[{"id": "todo-1", "content": "check", "status": "pending"}],
    )

    payload = store.read("sess-1")
    assert payload is not None
    assert payload["version"] == PROJECTION_VERSION
    page, has_more, cursor = store.page(payload, limit=2)
    assert [m.content for m in page] == ["u3", "u4"]
    assert has_more is True
    assert cursor == "3"

    older, older_has_more, older_cursor = store.page(payload, limit=2, before=cursor)
    assert [m.content for m in older] == ["u1", "u2"]
    assert older_has_more is True
    assert older_cursor == "1"


@pytest.mark.asyncio
async def test_projection_rebuilds_from_checkpoint_when_missing_or_stale(history_spaces, monkeypatch):
    settings, home, project = history_spaces
    chat = ChatService()
    chat.settings = settings
    chat.session_store = SessionStore(home)
    chat.history_projection = HistoryProjectionStore(home)
    sid = chat.session_store.create(str(project))
    chat.history_projection.path_for(sid).write_text(
        json.dumps({"version": 0, "messages": []}),
        encoding="utf-8",
    )

    class DummyGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "messages": [
                        HumanMessage(content="old", id="h1"),
                        AIMessage(content="new", id="a1"),
                    ],
                    "api_usage": {"total_tokens": 5},
                    "todos": [{"id": "t1", "content": "todo", "status": "pending"}],
                }
            )

    monkeypatch.setattr("app.services.chat_service.get_graph", lambda: DummyGraph())

    page = await chat.get_history_page(sid, limit=1)
    assert [m.content for m in page.messages] == ["new"]
    assert page.has_more is True
    assert page.next_cursor == "1"
    rebuilt = chat.history_projection.read(sid)
    assert rebuilt is not None
    assert rebuilt["version"] == PROJECTION_VERSION


@pytest.mark.asyncio
async def test_large_tool_result_history_page_uses_preview_and_ref(history_spaces):
    settings, home, project = history_spaces
    chat = ChatService()
    chat.settings = settings
    chat.session_store = SessionStore(home)
    sid = chat.session_store.create(str(project))
    large = "line\n" + ("x" * 5_000)
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "read_file",
                    "args": {"file_path": "big.txt"},
                }
            ],
        ),
        ToolMessage(content=large, tool_call_id="call-1", id="tool-1", name="read_file"),
    ]

    projected = chat.messages_to_openai(messages, session_id=sid, compact_tools=True)
    tool_msg = next(msg for msg in projected if msg.role == "tool")
    assert tool_msg.tool_call_id == "call-1"
    assert tool_msg.tool_result_ref == "call-1"
    assert tool_msg.tool_result_truncated is True
    assert tool_msg.tool_result_preview
    assert len(tool_msg.content or "") < len(large)

    full = await chat.get_tool_result(sid, "call-1")
    assert full.available is True
    assert full.content == large
