from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncio
import aiosqlite
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from app.agent import graph as graph_module
from app.agent.approval_context import approval_enabled
from app.agent.audit import get_runtime_audit
from app.agent.nodes import tools as tools_module
from app.agent.nodes.tools import _interaction_request
from app.agent.state import to_graph_state
from app.services.chat_service import ChatService
from app.services.ws_chat_service import WsChatService
from app.storage.checkpoint import CanonicalAsyncSqliteSaver
from app.storage.conversation_files import SessionConversationFiles
from app.storage.history_projection import project_ledger

SID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_sqlite_restart_and_ws_reconnect_resume_one_original_tool_once(tmp_path, monkeypatch):
    files = SessionConversationFiles(tmp_path)
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(graph_module, "get_tools", lambda: [object()])
    tool_executions = []
    def agent(state, _config):
        message = AIMessage(content="done", id="run--final") if any(isinstance(m, ToolMessage) for m in state["messages"]) else AIMessage(content="", id="run--definition", tool_calls=[{"id": "call1", "name": "write", "args": {"path": "file.txt"}}])
        return {"messages": [message], "working_messages": state["messages"], "step": state["step"] + 1}
    async def tools(_state, _config):
        identity, seq, step_id = _interaction_request("approval", "call1", prompt="approve")
        decision = interrupt({"kind": "approval", "interaction_id": identity, "interrupt_id": identity, "definition_seq": seq, "step_id": step_id, "tool_call_id": "call1"})
        assert decision == "allow"
        tool_executions.append("call1")
        get_runtime_audit().tool_started("call1", 1)
        return {"messages": [ToolMessage(content="ok", tool_call_id="call1", additional_kwargs={"tool_status": "succeeded", "attempt": 1})]}
    monkeypatch.setattr(graph_module, "call_agent", agent)
    monkeypatch.setattr(graph_module, "call_tools", tools)
    config = graph_module.make_thread_config(SID)
    state = graph_module.make_initial_state("write file", 4, session_id=SID, turn_id="turn1")
    state["ledger_pointer"]["applied_seq"] = files.append_messages(SID, "turn1", state["context"]["working_message"], scope=state["scope"])
    database = str(tmp_path / "checkpoint.sqlite")
    # Close the first sqlite connection and graph before creating the WS service.
    async with aiosqlite.connect(database) as connection:
        monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: CanonicalAsyncSqliteSaver(connection))
        graph = graph_module.build_graph()
        await graph.ainvoke(to_graph_state(state), config)
        waiting = dict((await graph.aget_state(config)).values)
        assert waiting["turn_status"] == "waiting_user"
        pending = waiting["pending_interaction"]
        saved_step = waiting["current_step"]["step_id"]
    async with aiosqlite.connect(database) as restarted_connection:
        monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: CanonicalAsyncSqliteSaver(restarted_connection))
        graph = graph_module.build_graph()
        monkeypatch.setattr("app.services.ws_chat_service.get_graph", lambda: graph)
        chat = ChatService.__new__(ChatService)
        chat.conversation_files = files
        chat._get_graph_bounded = AsyncMock(return_value=graph)
        chat._max_steps = lambda: 4
        chat.session_store = SimpleNamespace(touch=MagicMock())
        chat._schedule_background = MagicMock()
        manager = SimpleNamespace(send_event=AsyncMock(), subscribe_session=MagicMock())
        websocket = WsChatService(manager, chat)
        await websocket.handle_message(7, {"op": "session.subscribe", "session_id": SID})
        task = websocket._reattach_tasks["turn1"]
        await websocket.handle_message(7, {"op": "approve", "session_id": SID, "turn_id": "turn1", "step_id": saved_step, "tool_call_id": "call1", "interrupt_id": pending["id"], "decision": "allow"})
        await asyncio.wait_for(task, timeout=5)
        await websocket.handle_message(7, {"op": "approve", "session_id": SID, "turn_id": "turn1", "step_id": saved_step, "tool_call_id": "call1", "interrupt_id": pending["id"], "decision": "allow"})
        assert manager.send_event.await_args.args[1].data["reason_code"] == "stale_interrupt"
        completed = dict((await graph.aget_state(config)).values)
    assert tool_executions == ["call1"]
    assert completed["turn_status"] == "completed" and completed["scope"]["turn_id"] == "turn1"
    rows = files.read(SID)
    assert len([r for r in rows if r["message"]["role"] == "user" and not r.get("interaction")]) == 1
    assert len([r for r in rows if r.get("interaction", {}).get("decision") is not None]) == 1
    assert len([r for r in rows if r["message"]["role"] == "tool"]) == 1
    assert {r["message"]["id"] for r in rows if r["message"].get("id")} == {"run--definition", "run--final"}
    assert any(call.args[1].type == "done" for call in manager.send_event.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["approval_timeout", "question_timeout", "handoff"])
async def test_native_waiting_timeout_and_handoff_settle_one_interaction(tmp_path, monkeypatch, scenario):
    files = SessionConversationFiles(tmp_path)
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(graph_module, "get_tools", lambda: [object()])
    async def bind_workspace(_session):
        return tmp_path
    monkeypatch.setattr(tools_module, "abind_session_runtime", bind_workspace)
    monkeypatch.setattr(tools_module, "dispatch_pre_tool_call", lambda **_kwargs: None)
    policy = SimpleNamespace(outcome="needs_approval", reason="approve this action", reason_code="approval_required", preview=None)
    card = SimpleNamespace(name="protected_action")
    monkeypatch.setattr(tools_module, "_resolve_effective_card_and_args", lambda _name, args: (card, args, None))
    monkeypatch.setattr(tools_module, "build_execution_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(tools_module, "evaluate_policy", lambda _context: policy)
    side_effects = []
    def run_calls(*_args, **_kwargs):
        side_effects.append("ran")
        return []
    monkeypatch.setattr(tools_module, "run_tool_calls", run_calls)
    agent_calls = []
    def agent(state, _config):
        agent_calls.append(True)
        if any(isinstance(message, ToolMessage) for message in state["messages"]):
            message = AIMessage(content="finished", id="run--finished")
        else:
            if scenario == "approval_timeout":
                calls = [{"id": "call1", "name": "protected_action", "args": {}}]
            else:
                mode = "handoff_and_stop" if scenario == "handoff" else "answer_and_continue"
                calls = [{"id": "call1", "name": "ask_user", "args": {"question": "choose", "options": ["yes", "no"], "mode": mode}}]
                if scenario == "handoff":
                    calls.append({"id": "call2", "name": "protected_action", "args": {}})
            message = AIMessage(content="", id="run--interaction", tool_calls=calls)
        return {"messages": [message], "working_messages": state["messages"], "step": state["step"] + 1}
    monkeypatch.setattr(graph_module, "call_agent", agent)
    config = graph_module.make_thread_config(SID)
    state = graph_module.make_initial_state("ask before continuing", 4, session_id=SID, turn_id="turn1")
    state["ledger_pointer"]["applied_seq"] = files.append_messages(SID, "turn1", state["context"]["working_message"], scope=state["scope"])
    database = str(tmp_path / "checkpoint.sqlite")
    async with aiosqlite.connect(database) as connection:
        monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: CanonicalAsyncSqliteSaver(connection))
        graph = graph_module.build_graph()
        token = approval_enabled.set(True)
        try:
            await graph.ainvoke(to_graph_state(state), config)
        finally:
            approval_enabled.reset(token)
        waiting = dict((await graph.aget_state(config)).values)
        assert waiting["turn_status"] == "waiting_user"
        pending = waiting["pending_interaction"]
        saved_step = waiting["current_step"]["step_id"]
    actual_wait_for = asyncio.wait_for
    async def short_user_wait(awaitable, timeout):
        return await actual_wait_for(awaitable, 0.02 if timeout == 300.0 else timeout)
    monkeypatch.setattr("app.services.ws_chat_service.asyncio.wait_for", short_user_wait)
    async with aiosqlite.connect(database) as restarted_connection:
        monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: CanonicalAsyncSqliteSaver(restarted_connection))
        graph = graph_module.build_graph()
        monkeypatch.setattr("app.services.ws_chat_service.get_graph", lambda: graph)
        chat = ChatService.__new__(ChatService)
        chat.conversation_files = files
        chat._get_graph_bounded = AsyncMock(return_value=graph)
        chat._max_steps = lambda: 4
        chat.session_store = SimpleNamespace(touch=MagicMock())
        chat._schedule_background = MagicMock()
        manager = SimpleNamespace(send_event=AsyncMock(), subscribe_session=MagicMock())
        websocket = WsChatService(manager, chat)
        await websocket.handle_message(7, {"op": "session.subscribe", "session_id": SID})
        task = websocket._reattach_tasks["turn1"]
        if scenario == "handoff":
            await websocket.handle_message(7, {"op": "approve", "session_id": SID, "turn_id": "turn1", "step_id": saved_step, "tool_call_id": "call1", "interrupt_id": pending["id"], "kind": "ask_user", "mode": "handoff_and_stop", "ack": True})
        await actual_wait_for(task, timeout=5)
        completed = dict((await graph.aget_state(config)).values)
        if scenario != "handoff":
            emitted = [call.args[1] for call in manager.send_event.await_args_list]
            assert not any(event.type == "error" for event in emitted)
            assert any(event.type == "stage" and event.data.get("stage") == "interaction_timeout" for event in emitted)
            assert any(event.type == "done" for event in emitted)
        await websocket.handle_message(7, {"op": "approve", "session_id": SID, "turn_id": "turn1", "step_id": saved_step, "tool_call_id": "call1", "interrupt_id": pending["id"], "kind": "tool_approval" if scenario == "approval_timeout" else "ask_user", "answer": "yes", "decision": "allow"})
        assert manager.send_event.await_args.args[1].data["reason_code"] == "stale_interrupt"
    rows = files.read(SID)
    decisions = [row for row in rows if row.get("interaction", {}).get("decision") is not None]
    assert len(decisions) == 1 and not side_effects
    decision = decisions[0]
    expected_status = "completed" if scenario == "handoff" else "expired"
    assert decision["execution"]["status"] == expected_status
    assert type(decision["execution"]["finished_at"]) is int
    assert decision["turn_id"] == "turn1" and decision["message"]["tool_call_id"] == "call1"
    assert len([row for row in rows if row["message"]["role"] == "user" and not row.get("interaction")]) == 1
    timeline = project_ledger(rows)["timelines"][0]
    assert timeline["interactions"][0]["status"] == expected_status
    assert timeline["interactions"][0]["decision_seq"] == decision["seq"]
    tools = [row for row in rows if row["message"]["role"] == "tool"]
    assert all("attempt" not in row["execution"] and "started_at" not in row["execution"] for row in tools)
    if scenario == "handoff":
        assert completed["turn_status"] == timeline["status"] == "handoff"
        assert len(agent_calls) == 1
        assert [row["execution"]["status"] for row in tools] == ["succeeded", "skipped"]
        assert decision["message"]["role"] == "user" and decision["interaction"]["decision"]["ack"] is True
    else:
        assert completed["turn_status"] == timeline["status"] == "completed"
        assert len(agent_calls) == 2
        assert tools[0]["execution"]["status"] == ("denied" if scenario == "approval_timeout" else "failed")
        assert decision["message"]["role"] == "system" and decision["interaction"]["decision"]["source"] == "timeout"
