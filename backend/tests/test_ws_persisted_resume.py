import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.audit import RuntimeLedgerAudit
from app.agent.state import new_agent_state
from app.agent.streaming.events import AgentEvent
from app.services.chat_service import ChatService
from app.services.ws_chat_service import WsChatService
from app.storage.conversation_files import SessionConversationFiles


def persisted_question():
    state = new_agent_state(workspace_id="workspace", session_id="session1", turn_id="turn1", message=HumanMessage(content="original question"))
    state["turn_status"] = "waiting_user"
    state["current_step"] = {"step_id": "step1", "step_name": "tools", "step_status": "waiting_user", "tool_calls": [{"call_id": "call1", "definition_seq": 1, "status": "pending"}]}
    state["pending_interaction"] = {"id": "interaction1", "kind": "ask_user", "call_id": "call1", "definition_seq": 2, "mode": "answer_and_continue"}
    native = {"kind": "ask_user", "interrupt_id": "interaction1", "tool_call_id": "call1", "step_id": "step1", "options": ["yes", "no"], "mode": "answer_and_continue", "prompt": "choose"}
    snapshot = SimpleNamespace(values=state, tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value=native)])])
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot))
    manager = SimpleNamespace(send_event=AsyncMock(), subscribe_session=MagicMock())
    calls = []

    async def resume(session, decision, *, turn_id):
        calls.append((session, decision, turn_id))
        snapshot.tasks = []
        state["pending_interaction"] = None
        state["turn_status"] = "completed"
        yield AgentEvent("done", {"content": "done", "session_id": session}, turn_id)

    chat = SimpleNamespace(_recover_ledger_tail=lambda _session, value: value, resume_chat_events=resume,
                           iter_chat_events=AsyncMock(), repair_cancelled_checkpoint=AsyncMock(), mark_terminal_checkpoint=AsyncMock())
    service = WsChatService(manager, chat)
    return service, state, snapshot, graph, calls


@pytest.mark.asyncio
async def test_subscribe_reattaches_after_restart_and_immediate_answer_is_not_lost():
    service, state, _snapshot, graph, calls = persisted_question()
    with patch("app.services.ws_chat_service.get_graph", return_value=graph):
        await service.handle_message(7, {"op": "session.subscribe", "session_id": "session1"})
        task = service._reattach_tasks["turn1"]
        await service.handle_message(7, {"op": "approve", "session_id": "session1", "turn_id": "turn1", "step_id": "step1", "tool_call_id": "call1", "interrupt_id": "interaction1", "kind": "ask_user", "mode": "answer_and_continue", "answer": "yes"})
        await asyncio.wait_for(task, timeout=1)
    assert len(calls) == 1 and calls[0][0] == "session1" and calls[0][2] == "turn1"
    assert calls[0][1]["answer"] == "yes"
    service.chat_service.iter_chat_events.assert_not_called()
    assert state["turn_status"] == "completed"
    assert not service._active_runs and not service._run_sessions
    assert "interrupt" in [call.args[1].type for call in service.manager.send_event.await_args_list]


@pytest.mark.asyncio
async def test_approve_can_reattach_directly_and_never_starts_new_user_turn():
    service, _state, _snapshot, graph, calls = persisted_question()
    with patch("app.services.ws_chat_service.get_graph", return_value=graph):
        await service.handle_message(7, {"op": "approve", "session_id": "session1", "turn_id": "turn1", "step_id": "step1", "tool_call_id": "call1", "interrupt_id": "interaction1", "kind": "ask_user", "mode": "answer_and_continue", "answer": "no"})
        task = service._reattach_tasks["turn1"]
        await asyncio.wait_for(task, timeout=1)
    assert len(calls) == 1 and calls[0][1]["answer"] == "no"
    service.chat_service.iter_chat_events.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_turn_or_corrupted_native_interaction_cannot_reattach():
    service, state, snapshot, graph, calls = persisted_question()
    with patch("app.services.ws_chat_service.get_graph", return_value=graph):
        assert await service._attach_waiting_turn(7, "session1", "old-turn") is False
        assert not service._approval_futures and not service._active_runs
        snapshot.tasks[0].interrupts[0].value["interrupt_id"] = "other-interaction"
        assert await service._attach_waiting_turn(7, "session1", "turn1") is False
    assert not calls and not service._active_runs


@pytest.mark.asyncio
async def test_already_durable_answer_resumes_once_without_asking_again():
    service, state, _snapshot, graph, calls = persisted_question()
    state["pending_interaction"]["decision"] = {"kind": "ask_user", "interrupt_id": "interaction1", "mode": "answer_and_continue", "answer": "yes"}
    with patch("app.services.ws_chat_service.get_graph", return_value=graph):
        assert await service._attach_waiting_turn(7, "session1")
        task = service._reattach_tasks["turn1"]
        await asyncio.wait_for(task, timeout=1)
    assert len(calls) == 1 and calls[0][1]["answer"] == "yes"
    assert "interrupt" not in [call.args[1].type for call in service.manager.send_event.await_args_list]


@pytest.mark.asyncio
async def test_terminal_persisted_turn_state_sync_works_without_in_memory_run():
    service, state, snapshot, graph, calls = persisted_question()
    state["turn_status"] = "completed"
    state["pending_interaction"] = None
    snapshot.tasks = []
    with patch("app.services.ws_chat_service.get_graph", return_value=graph):
        await service.handle_message(7, {"op": "state.sync", "session_id": "session1", "turn_id": "turn1"})
    assert not service._active_runs and not calls
    event = service.manager.send_event.await_args.args[1]
    assert event.type == "state_sync" and event.state["turn_status"] == "completed"


@pytest.mark.asyncio
async def test_subscribe_and_state_sync_do_not_recover_an_actively_running_tool(tmp_path):
    session_id = "00000000-0000-4000-8000-000000000001"
    files = SessionConversationFiles(tmp_path)
    state = new_agent_state(workspace_id=str(tmp_path), session_id=session_id, turn_id="turn1", message=HumanMessage(content="run tool"))
    files.append_messages(session_id, "turn1", state["context"]["working_message"], scope=state["scope"])
    definition = AIMessage(content="", tool_calls=[{"id": "call1", "name": "write", "args": {}}])
    definition_seq = files.append_messages(session_id, "turn1", [definition], scope=state["scope"])
    state["current_step"] = {"step_id": "step1", "step_name": "tools", "step_status": "running", "tool_calls": [{"call_id": "call1", "definition_seq": definition_seq, "status": "pending"}]}
    state["turn_status"] = "running"
    state["ledger_pointer"] = files.ledger_pointer(session_id, definition_seq)
    audit = RuntimeLedgerAudit(files, state["scope"], step_id="step1")
    audit.tool_started("call1", 1)
    original_seq = files.latest_seq(session_id)
    chat = ChatService.__new__(ChatService)
    chat.conversation_files = files
    chat._recover_ledger_tail = MagicMock(wraps=chat._recover_ledger_tail)
    manager = SimpleNamespace(send_event=AsyncMock(), subscribe_session=MagicMock())
    service = WsChatService(manager, chat)
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values=state, tasks=[])))
    task = asyncio.create_task(asyncio.Event().wait())
    service._active_runs["turn1"] = task
    service._run_sessions["turn1"] = session_id
    try:
        with patch("app.services.ws_chat_service.get_graph", return_value=graph):
            await service.handle_message(7, {"op": "session.subscribe", "session_id": session_id})
            await service.handle_message(7, {"op": "state.sync", "session_id": session_id, "turn_id": "turn1"})
        chat._recover_ledger_tail.assert_not_called()
        assert files.latest_seq(session_id) == original_seq
        assert not any(row.get("execution", {}).get("status") == "unknown" for row in files.read(session_id))
        event = manager.send_event.await_args.args[1]
        assert event.type == "state_sync" and event.state["turn_status"] == "running"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
