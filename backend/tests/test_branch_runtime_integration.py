from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent import graph as graph_module
from app.agent.audit import get_runtime_audit, RuntimeLedgerAudit
from app.agent.graph import make_thread_config
from app.core.exceptions import AppError
from app.core.settings import Settings, init_workspace
from app.schemas.chat import ChatResponse
from app.services.chat_service import ChatService
from app.services.ws_chat_service import WsChatService
from app.storage.event_projection import project_events
from app.storage.history_projection import project_ledger
from app.storage.conversation_files import SessionConversationFiles
from app.schemas.session import ChatMessage


@pytest.fixture
def branch_runtime(monkeypatch, tmp_path):
    settings = Settings(runtime_root=tmp_path / "runtime", openai_api_key="test-key")
    init_workspace(settings)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.storage.project.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)
    monkeypatch.setattr(graph_module, "get_tools", lambda: [])
    monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: MemorySaver())
    invocations = []

    def agent(state, _config):
        invocations.append([message.content for message in state["messages"]])
        request_id = f"request-{len(invocations)}"
        get_runtime_audit().model_started(request_id)
        last_user = next(message.content for message in reversed(state["messages"]) if isinstance(message, HumanMessage))
        answer = AIMessage(content=f"answer: {last_user}", additional_kwargs={"request_id": request_id})
        return {"messages": [answer], "request_id": request_id, "working_messages": state["messages"],
                "step": int(state["step"]) + 1}

    monkeypatch.setattr(graph_module, "call_agent", agent)
    graph = graph_module.build_graph()
    monkeypatch.setattr("app.services.chat_service.get_graph", lambda: graph)

    async def prepare(_message):
        return None

    monkeypatch.setattr("app.services.chat_service.prepare_session_memory", prepare)
    monkeypatch.setattr("app.services.chat_service.dispatch_post_llm_call", lambda **_kwargs: None)
    monkeypatch.setattr("app.services.chat_service.dispatch_observe", lambda *_args, **_kwargs: None)
    chat = ChatService()
    chat._schedule_background = lambda _name, _factory: None
    sid = chat.session_store.create(str(project))
    return chat, graph, sid, invocations


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_historical_continuation_writes_child_and_keeps_parent_checkpoint(branch_runtime, stream):
    chat, graph, sid, calls = branch_runtime
    await chat.send_sync("first", sid, turn_id="main-turn-1", submission_id="first")
    first_events = chat.conversation_files.read_events(sid)
    fork_point = next(event for event in reversed(first_events) if event["event_type"] == "turn.settled")
    await chat.send_sync("later main", sid, turn_id="main-turn-2", submission_id="later-main")
    parent_state = dict((await graph.aget_state(make_thread_config(sid))).values)
    parent_events = chat.conversation_files.read_events(sid)
    parent_bytes = chat.conversation_files.ledger_path(sid).read_bytes()
    cursor = {key: fork_point[key] for key in ("branch_id", "event_seq", "event_id")}

    if stream:
        frames = [event async for event in chat.iter_chat_events(
            "child question", sid, turn_id="child-turn-1", submission_id="child-first",
            branch_id="child", fork_cursor=cursor,
        )]
        assert any(frame.type == "done" and frame.data.get("content") == "answer: child question" for frame in frames)
        durable = [frame for frame in frames if frame.type == "timeline_event"]
        assert durable[0].data["event"]["event_type"] == "branch.forked"
        assert all(frame.branch_id == "child" for frame in durable)
    else:
        response = await chat.send_sync(
            "child question", sid, turn_id="child-turn-1", submission_id="child-first",
            branch_id="child", fork_cursor=cursor,
        )
        assert response.content == "answer: child question" and response.branch_id == "child"

    events = chat.conversation_files.read_events(sid)
    added = events[len(parent_events):]
    assert added[0]["event_type"] == "branch.forked"
    assert added[0]["payload"]["parent_cursor"] == cursor
    assert all(event["branch_id"] == "child" for event in added)
    assert calls[-1] == ["first", "answer: first", "child question"]
    assert chat.conversation_files.ledger_path(sid).read_bytes().startswith(parent_bytes)
    assert dict((await graph.aget_state(make_thread_config(sid))).values) == parent_state
    child_state = dict((await graph.aget_state(make_thread_config(sid, "child"))).values)
    assert child_state["ledger_pointer"]["event_cursor"]["branch_id"] == "child"
    assert child_state["scope"]["turn_id"] == "child-turn-1"
    assert child_state["pending_interaction"] is None
    terminal = next(event for event in reversed(added) if event["event_type"] == "turn.settled")
    response = next(event for event in reversed(added) if event["event_type"] == "model.response.completed")
    assert terminal["payload"]["final_response_event_id"] == response["event_id"]
    assert terminal["payload"]["final_message_id"] == response["payload"]["message_id"]

    await chat.send_sync("child second", sid, turn_id="child-turn-2", submission_id="child-second", branch_id="child")
    assert calls[-1] == ["first", "answer: first", "child question", "answer: child question", "child second"]
    main = project_events(chat.conversation_files.read_events(sid), branch_id="main")
    assert [main["messages"][key]["content"] for key in main["message_order"]] == [
        "first", "answer: first", "later main", "answer: later main",
    ]


@pytest.mark.asyncio
async def test_historical_tool_prefix_is_rejected_without_reexecution_or_fork(branch_runtime):
    chat, graph, sid, calls = branch_runtime
    await chat.send_sync("first", sid, turn_id="main-turn-1", submission_id="first")
    seq, _ = chat.conversation_files.append(
        sid, "unfinished-definition", {"role": "assistant", "tool_calls": [
            {"id": "unsafe-call", "name": "shell", "arguments": {"command": "side effect"}},
        ]}, turn_id="old-incomplete-turn", model={"request_id": "old-request"},
    )
    unfinished = chat.conversation_files.read_events(sid)[seq - 1]
    count = len(calls)
    with pytest.raises(AppError) as exc:
        await chat.send_sync(
            "continue", sid, branch_id="child", submission_id="child-submission",
            fork_cursor={key: unfinished[key] for key in ("branch_id", "event_seq", "event_id")},
        )
    assert exc.value.code == "historical_tool_state_incomplete"
    assert len(calls) == count
    assert not chat.conversation_files.read_events(sid, branch_id="child")
    assert not (await graph.aget_state(make_thread_config(sid, "child"))).values


@pytest.mark.asyncio
async def test_legacy_history_and_single_session_cache_always_show_main(branch_runtime):
    chat, graph, sid, _calls = branch_runtime
    await chat.send_sync("main question", sid, turn_id="main-turn", submission_id="main-submission")
    point = chat.conversation_files.read_events(sid)[-1]
    await chat.send_sync("child question", sid, turn_id="child-turn", submission_id="child-submission",
                         branch_id="child", fork_cursor={key: point[key] for key in ("branch_id", "event_seq", "event_id")})
    expected = ["main question", "answer: main question"]
    # A child turn invokes this helper with its own checkpoint configuration.
    await chat._refresh_history_projection(sid, graph=graph, config=make_thread_config(sid, "child"))
    assert [message.content for message in (await chat.get_history(sid)).messages] == expected
    assert [message.content for message in (await chat.get_history_page(sid)).messages] == expected
    # Rebuild caches produced before branch ownership was recorded.
    mixed = project_ledger(chat.conversation_files.read(sid))
    chat.history_projection.write(sid, [ChatMessage(**row) for row in mixed["messages"]],
                                  ledger_seq=chat.conversation_files.latest_seq(sid))
    import json
    path = chat.history_projection.path_for(sid)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("branch_id")
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert [message.content for message in (await chat.get_history_page(sid)).messages] == expected
    assert chat.history_projection.read(sid)["branch_id"] == "main"


def test_tool_start_summary_uses_visible_branch_definition_and_redacts_secrets(tmp_path):
    files = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000001"
    files.fork_branch(sid, "child", parent_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"})
    files.append(sid, "child-definition", {"role": "assistant", "tool_calls": [
        {"id": "shared-call", "name": "run_shell", "arguments": {"command": "pytest", "api_key": "SECRET"}},
    ]}, turn_id="turn", branch_id="child")
    files.append(sid, "main-definition", {"role": "assistant", "tool_calls": [
        {"id": "shared-call", "name": "wrong_branch_tool", "arguments": {}},
    ]}, turn_id="turn")
    scope = {"workspace_id": files.workspace_id, "session_id": sid, "turn_id": "turn"}
    audit = RuntimeLedgerAudit(files, scope, branch_id="child", step_id="step", task_id="task")
    seq = audit.tool_started("shared-call", 1)
    event = files.read_events(sid, after_seq=seq - 1)[0]
    runtime = event["payload"]["extensions"]["runtime"]
    assert event["branch_id"] == "child" and runtime["tool_name"] == "run_shell"
    assert "pytest" in runtime["input_summary"] and "SECRET" not in runtime["input_summary"]
    assert runtime["step_id"] == "step" and runtime["task_id"] == "task"


@pytest.mark.asyncio
async def test_large_final_response_retry_resolves_content_without_repeating_model(branch_runtime, monkeypatch):
    chat, _graph, sid, calls = branch_runtime
    answer = "large answer " * 7000

    def large_answer(state, _config):
        calls.append([message.content for message in state["messages"]])
        request_id = "large-request"
        get_runtime_audit().model_started(request_id)
        return {"messages": [AIMessage(content=answer, additional_kwargs={"request_id": request_id})],
                "request_id": request_id, "working_messages": state["messages"], "step": int(state["step"]) + 1}

    monkeypatch.setattr(graph_module, "call_agent", large_answer)
    response = await chat.send_sync("large question", sid, turn_id="large-turn", submission_id="large-submission")
    assert response.content == answer
    stored_response = next(event for event in chat.conversation_files.read_events(sid)
                           if event["event_type"] == "model.response.completed")
    assert stored_response["payload"]["message"]["content"][0]["type"] == "reference"
    events_before_retry = chat.conversation_files.latest_seq(sid)
    repeated = await chat.send_sync("large question", sid, turn_id="retry-turn", submission_id="large-submission")
    assert repeated.content == answer and repeated.turn_id == "large-turn"
    frames = [frame async for frame in chat.iter_chat_events(
        "large question", sid, turn_id="stream-retry-turn", submission_id="large-submission",
    )]
    done = next(frame for frame in frames if frame.type == "done")
    assert done.data["content"] == answer and done.data["duplicate_submission"]
    assert len(calls) == 1 and chat.conversation_files.latest_seq(sid) == events_before_retry


def test_chat_http_forwards_branch_and_fork_cursor(monkeypatch):
    from app.api import chat as chat_api
    from app.main import app
    received = []

    class FakeChat:
        async def send_sync(self, message, session_id, **kwargs):
            received.append(("sync", message, session_id, kwargs))
            return ChatResponse(content="done", session_id=session_id, turn_id=kwargs["turn_id"], branch_id=kwargs["branch_id"])

        async def send_stream(self, message, session_id, **kwargs):
            received.append(("stream", message, session_id, kwargs))
            yield 'event: done\ndata: {"content":"done"}\n\n'

    monkeypatch.setattr(chat_api, "chat_service", FakeChat())
    body = {"message": "continue", "session_id": "session-1", "turn_id": "child-turn",
            "submission_id": "submission-1", "branch_id": "child",
            "fork_cursor": {"branch_id": "main", "event_seq": 3, "event_id": "event-3"}}
    with TestClient(app) as client:
        assert client.post("/api/chat/send", json=body).json()["branch_id"] == "child"
        assert client.post("/api/chat/send/stream", json=body).status_code == 200
        assert client.post("/api/chat/send", json={**body, "branch_id": "main"}).status_code == 422
    assert [entry[0] for entry in received] == ["sync", "stream"]
    assert all(entry[3]["branch_id"] == "child" and entry[3]["fork_cursor"] == body["fork_cursor"] for entry in received)


@pytest.mark.asyncio
async def test_websocket_forwards_branch_and_validates_fork_cursor():
    manager = SimpleNamespace(send_event=AsyncMock())
    service = WsChatService(manager, chat_service=SimpleNamespace())
    service._start_run = AsyncMock()
    payload = {"op": "chat.send", "message": "continue", "session_id": "session-1",
               "turn_id": "child-turn", "submission_id": "submission-1", "branch_id": "child",
               "fork_cursor": {"branch_id": "main", "event_seq": 3, "event_id": "event-3"}}

    await service.handle_message(7, payload)

    service._start_run.assert_awaited_once_with(7, "child-turn", "continue", "session-1", "submission-1",
                                              branch_id="child", fork_cursor=payload["fork_cursor"])
    service._start_run.reset_mock()
    await service.handle_message(7, {**payload, "branch_id": "main"})
    service._start_run.assert_not_awaited()
    assert manager.send_event.await_args.args[1].data["reason_code"] == "invalid_branch_cursor"
