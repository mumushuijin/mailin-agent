from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.core.settings import Settings, init_workspace
from app.agent.state import REQUIRED_TOP_LEVEL
from app.schemas.session import ChatMessage
from app.services.chat_service import ChatService
from app.storage.history_projection import HistoryProjectionStore, PROJECTION_VERSION
from app.storage.conversation_files import SessionConversationFiles
from app.storage.workspace import SessionStore


@pytest.fixture
def history_spaces(tmp_path: Path, monkeypatch):
    home = tmp_path / "runtime" / "data" / "agent-home"
    project = tmp_path / "project"
    home.mkdir(parents=True)
    project.mkdir()
    settings = Settings(
        runtime_root=tmp_path / "runtime",
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
    projection_path = chat.history_projection.path_for(sid)
    projection_path.parent.mkdir(parents=True, exist_ok=True)
    projection_path.write_text(
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
async def test_projection_rebuilds_from_jsonl_ledger(history_spaces):
    settings, home, project = history_spaces
    chat = ChatService()
    chat.settings = settings
    chat.session_store = SessionStore(home)
    chat.history_projection = HistoryProjectionStore(home)
    chat.conversation_files = SessionConversationFiles(home)
    sid = chat.session_store.create(str(project))
    scope = {"workspace_id": "workspace-1", "session_id": sid, "run_id": "run-1",
             "task_id": "task-1", "request_id": None, "step_id": None}
    chat.conversation_files.append_messages(
        sid, "run-1", [HumanMessage(content="question", id="m1"), AIMessage(content="answer", id="m2")], scope=scope,
    )
    page = await chat.get_history_page(sid, limit=10)
    assert [message.content for message in page.messages] == ["question", "answer"]
    assert [message.scope["run_id"] for message in page.messages] == ["run-1", "run-1"]
    assert len({message.message_id for message in page.messages}) == 2
    assert page.messages[1].vendor_message_id == "m2"
    assert chat.history_projection.read(sid) is not None
    chat.conversation_files.append_messages(sid, "run-2", [HumanMessage(content="follow up", id="m3")], scope={**scope, "run_id": "run-2", "task_id": "task-2"})
    refreshed = await chat.get_history_page(sid, limit=10)
    assert [message.content for message in refreshed.messages] == ["question", "answer", "follow up"]


@pytest.mark.asyncio
async def test_five_runs_eleven_model_replies_keep_scoped_history_without_prompt_rows(history_spaces):
    settings, home, project = history_spaces
    chat = ChatService()
    chat.settings = settings
    chat.session_store = SessionStore(home)
    chat.history_projection = HistoryProjectionStore(home)
    chat.conversation_files = SessionConversationFiles(home)
    sid = chat.session_store.create(str(project))
    for run_index in range(5):
        run_id = f"run-{run_index}"
        scope = {"workspace_id": "workspace-1", "session_id": sid, "run_id": run_id,
                 "task_id": f"task-{run_index}", "request_id": None, "step_id": None}
        chat.conversation_files.append_messages(sid, run_id, [HumanMessage(content=f"question {run_index}")], scope=scope)
        for request_index in range(2 + (run_index == 0)):
            request_scope = {**scope, "request_id": f"request-{run_index}-{request_index}",
                             "step_id": f"step-{run_index}-{request_index}"}
            messages = [
                SystemMessage(content="rebuilt bootstrap", additional_kwargs={"context_bootstrap": True}),
                AIMessage(content=f"answer {run_index}-{request_index}", id=f"run--vendor-{run_index}-{request_index}"),
            ]
            if run_index in {1, 3} and request_index == 0:
                call_id = f"call-{run_index}"
                messages[-1].tool_calls = [{"id": call_id, "name": "read_file", "args": {}}]
                messages.append(ToolMessage(content="result", tool_call_id=call_id, name="read_file"))
            chat.conversation_files.append_messages(sid, run_id, messages, scope=request_scope)

    rows = chat.conversation_files.read(sid)
    assert len(rows) == 18  # five users, eleven assistant replies, two tool results
    assert all(row["origin"] != "system_maintenance" for row in rows)
    first = await chat.get_history_page(sid, limit=30)
    chat.history_projection.delete(sid)
    rebuilt = await chat.get_history_page(sid, limit=30)
    assert [(m.message_id, m.scope, m.origin) for m in rebuilt.messages] == [
        (m.message_id, m.scope, m.origin) for m in first.messages
    ]
    assert [m.scope["run_id"] for m in rebuilt.messages if m.role == "user"] == [f"run-{i}" for i in range(5)]
    assert len({m.message_id for m in rebuilt.messages}) == 18


def test_history_projection_hides_legacy_prompt_frames_but_keeps_human_messages(history_spaces):
    settings, home, _project = history_spaces
    chat = ChatService()
    chat.settings = settings
    chat.conversation_files = SessionConversationFiles(home)
    projected = chat.messages_to_openai(
        [
            SystemMessage(content="人格提示"),
            HumanMessage(content="参考信息", additional_kwargs={"context_reference": True}),
            HumanMessage(content="旧维护提示", additional_kwargs={"system_maintenance": True}),
            HumanMessage(content="用户真的说的话"),
            AIMessage(content=""),
            AIMessage(content="助手回复"),
        ]
    )
    assert [(message.role, message.content) for message in projected] == [
        ("user", "用户真的说的话"),
        ("assistant", "助手回复"),
    ]


def test_new_run_carries_transcript_but_only_appends_current_user_message(history_spaces, monkeypatch):
    settings, home, project = history_spaces
    chat = ChatService()
    chat.settings = settings
    chat.conversation_files = SessionConversationFiles(home)
    session_id = "00000000-0000-4000-8000-000000000010"
    monkeypatch.setattr(chat, "_bootstrap_fingerprint", lambda _session_id: "fingerprint")
    previous = {
        "checkpoint_id": "cp_old",
        "scope": {},
        "context": {
            "working_message": [
                SystemMessage(content="system prompt"),
                HumanMessage(content="reference", additional_kwargs={"context_reference": True}),
                HumanMessage(content="prior user turn", id="prior-user"),
            ]
        },
        "max_step_every_run": 48,
        "tasks": [],
        "current_task": None,
        "current_step": {},
        "memory": {},
        "state_revision": 1,
    }
    assert REQUIRED_TOP_LEVEL.issubset(previous)

    state = chat._new_run_state(session_id, "current user turn", 48, "run-new", previous)

    assert [message.content for message in state["context"]["working_message"]] == [
        "prior user turn",
        "current user turn",
    ]
    ledger_messages = chat.conversation_files.messages(session_id)
    assert [message.content for message in ledger_messages] == ["current user turn"]


@pytest.mark.asyncio
async def test_tool_history_shows_execution_status_without_result_content(history_spaces):
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

    from app.context.tool_cache import save_tool_result

    save_tool_result(sid, "call-1", large, project)
    projected = chat.messages_to_openai(messages, session_id=sid, compact_tools=True)
    tool_msg = next(msg for msg in projected if msg.role == "tool")
    assert tool_msg.tool_call_id == "call-1"
    assert tool_msg.tool_status == "completed"
    assert tool_msg.content is None
    assert tool_msg.tool_result_preview is None

    full = await chat.get_tool_result(sid, "call-1")
    assert full.available is True
    assert full.content == large
