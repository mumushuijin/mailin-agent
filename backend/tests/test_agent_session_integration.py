"""Exercise a new session through the graph and the shared stream contract."""

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent import graph as graph_module
from app.agent.graph import make_thread_config
from app.agent.streaming.events import to_sse, to_ws
from app.core.settings import Settings, init_workspace
from app.services.chat_service import ChatService


@pytest.mark.asyncio
async def test_new_session_tool_stream_recovery_and_transports(monkeypatch, tmp_path):
    settings = Settings(runtime_root=tmp_path / "runtime", openai_api_key="test-key")
    init_workspace(settings)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.storage.project.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)
    monkeypatch.setattr(graph_module, "get_tools", lambda: [object()])
    monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: MemorySaver())

    calls = []

    def agent(state, _config):
        calls.append("agent")
        answer = (
            AIMessage(content="final answer")
            if any(isinstance(message, ToolMessage) for message in state["messages"])
            else AIMessage(content="", tool_calls=[
                {"id": "call-1", "name": "read_file", "args": {"file_path": "a.txt"}}
            ])
        )
        request_id = f"request-{len(calls)}"
        answer.additional_kwargs["request_id"] = request_id
        return {"messages": [answer], "request_id": request_id, "working_messages": state["messages"], "step": int(state["step"]) + 1}

    async def tools(state, _config):
        calls.append("tools")
        return {"messages": [ToolMessage(content="contents", tool_call_id="call-1", name="read_file")]}

    monkeypatch.setattr(graph_module, "call_agent", agent)
    monkeypatch.setattr(graph_module, "call_tools", tools)
    graph = graph_module.build_graph()
    monkeypatch.setattr("app.services.chat_service.get_graph", lambda: graph)
    monkeypatch.setattr("app.services.chat_service.prepare_session_memory", lambda _message: _done())
    monkeypatch.setattr("app.services.chat_service.dispatch_post_llm_call", lambda **_kwargs: None)
    monkeypatch.setattr("app.services.chat_service.dispatch_observe", lambda *_args, **_kwargs: None)

    chat = ChatService()
    chat._schedule_background = lambda _name, _factory: None
    sid = chat.session_store.create(str(project))
    events = [event async for event in chat.iter_chat_events("question", sid, run_id="run-1")]

    assert calls == ["agent", "tools", "agent"]
    assert any(event.type == "step_start" for event in events)
    assert any(event.type == "step_finish" for event in events)
    assert any(event.type == "tool_start" for event in events)
    assert any(event.type == "tool_finish" for event in events)
    assert any(event.type == "done" and event.data.get("content") == "final answer" for event in events)
    for event in events:
        ws = to_ws(event)
        sse = to_sse(event)
        assert json.loads(sse.split("data: ", 1)[1])["event_id"] == ws["event_id"]
        assert json.loads(sse.split("data: ", 1)[1]).get("state") == ws.get("state")

    snapshot = await graph.aget_state(make_thread_config(sid))
    state = dict(snapshot.values)
    assert state["run_status"] == "completed"
    assert state["context"]["working_message"][-1].content == "final answer"
    rows = chat.conversation_files.read(sid)
    assert rows and rows[-1]["message"]["data"]["content"] == "final answer"
    assert [row["origin"] for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert len({row["scope"]["run_id"] for row in rows}) == 1
    assert rows[0]["scope"]["request_id"] is None
    assert rows[1]["scope"]["request_id"] != rows[3]["scope"]["request_id"]
    assert rows[2]["scope"]["request_id"] is None
    assert rows[1]["scope"]["step_id"] != rows[2]["scope"]["step_id"] != rows[3]["scope"]["step_id"]
    assert not any(row["message"]["type"] == "system" for row in rows)
    old_fingerprint = state["context"].get("bootstrap_fingerprint")
    monkeypatch.setattr(chat, "_bootstrap_fingerprint", lambda _session: "new-bootstrap-version")
    refreshed = chat._refresh_stale_context(sid, state)
    assert refreshed["context"]["bootstrap_fingerprint"] != old_fingerprint
    assert refreshed["ledger_pointer"]["applied_seq"] == rows[-1]["seq"]
    assert len(chat.conversation_files.read(sid)) == len(rows)
    pointer = chat.conversation_files.write_summary(sid, "summary-1", {"content": "saved summary"})
    assert chat.conversation_files.read_summary(sid, pointer)["content"] == "saved summary"
    chat.conversation_files.append(
        sid, "tail-1", {"type": "human", "data": {"content": "tail"}},
        scope={**state["scope"], "request_id": None, "step_id": None}, origin="user",
    )
    recovered = chat._recover_ledger_tail(sid, state)
    assert recovered["ledger_pointer"]["applied_seq"] == rows[-1]["seq"] + 1


async def _done():
    return None
