import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ws_chat_service import WsChatService


def waiting(kind="ask_user", mode="answer_and_continue"):
    manager = SimpleNamespace(send_event=AsyncMock())
    service = WsChatService(manager, SimpleNamespace())
    future = asyncio.get_running_loop().create_future()
    service._approval_futures["turn1"] = future
    service._run_sessions["turn1"] = "session1"
    service._pending_interrupt_meta["turn1"] = {"kind": kind, "interrupt_id": "interaction1", "step_id": "step1", "tool_call_id": "call1", "session_id": "session1", "mode": mode, "options": ["yes", "no"]}
    payload = {"op": "approve", "turn_id": "turn1", "session_id": "session1", "step_id": "step1", "interrupt_id": "interaction1", "tool_call_id": "call1", "kind": "ask_user" if kind == "ask_user" else "tool_approval", "mode": mode, "answer": "yes", "decision": "allow"}
    return service, future, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("field,replacement,reason", [
    ("session_id", "session-old", "stale_session"),
    ("step_id", "step-old", "stale_step"),
    ("interrupt_id", "interaction-old", "stale_interrupt"),
    ("tool_call_id", "call-old", "stale_tool_call"),
    ("kind", "tool_approval", "stale_interaction_kind"),
    ("mode", "handoff_and_stop", "stale_interaction_mode"),
])
async def test_mismatched_response_cannot_consume_pending_question(field, replacement, reason):
    service, future, payload = waiting()
    await service.handle_message(1, {**payload, field: replacement})
    assert not future.done()
    assert service._approval_futures["turn1"] is future
    assert service.manager.send_event.await_args.args[1].data["reason_code"] == reason


@pytest.mark.asyncio
async def test_missing_interaction_id_and_invalid_answer_do_not_resume():
    service, future, payload = waiting()
    await service.handle_message(1, {key: value for key, value in payload.items() if key != "interrupt_id"})
    assert not future.done()
    await service.handle_message(1, {**payload, "answer": "not an option"})
    assert not future.done()
    assert service.manager.send_event.await_args.args[1].data["reason_code"] == "invalid_answer"


@pytest.mark.asyncio
async def test_valid_answer_and_handoff_preserve_current_interaction():
    service, future, payload = waiting()
    await service.handle_message(1, payload)
    assert future.result()["answer"] == "yes"
    assert future.result()["interrupt_id"] == "interaction1"
    assert service._approval_futures["turn1"].done()
    handoff_service, handoff_future, handoff = waiting(mode="handoff_and_stop")
    await handoff_service.handle_message(1, handoff)
    assert handoff_future.result()["ack"] is True


@pytest.mark.asyncio
async def test_approval_cannot_be_answered_with_question_kind_and_duplicate_decision_is_rejected():
    service, future, payload = waiting(kind="approval")
    await service.handle_message(1, {**payload, "kind": "ask_user"})
    assert not future.done()
    await service.handle_message(1, payload)
    assert future.result()["decision"] == "allow"
    await service.handle_message(1, payload)
    assert service.manager.send_event.await_args.args[1].data["reason_code"] == "stale_interrupt"


@pytest.mark.asyncio
async def test_cancelling_real_active_task_still_cleans_its_session_processes():
    chat = SimpleNamespace(repair_cancelled_checkpoint=AsyncMock(), mark_terminal_checkpoint=AsyncMock())
    service = WsChatService(SimpleNamespace(send_event=AsyncMock()), chat)
    service._run_sessions["t"] = "session1"
    entered = asyncio.Event()
    async def active():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            service._run_sessions.pop("t", None)
            service._active_runs.pop("t", None)
    task = asyncio.create_task(active())
    service._active_runs["t"] = task
    await entered.wait()
    with patch("app.tools.packages.shell.process_registry.kill_session_processes") as kill:
        await service.cancel_run("t")
    kill.assert_called_once_with("session1")
    chat.repair_cancelled_checkpoint.assert_awaited_once_with("session1")


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["session1", None])
async def test_immediate_cancel_before_coroutine_starts_still_settles_turn(session_id):
    chat = SimpleNamespace(repair_cancelled_checkpoint=AsyncMock(), mark_terminal_checkpoint=AsyncMock())
    service = WsChatService(SimpleNamespace(send_event=AsyncMock()), chat)
    started = []
    async def active(*_args):
        started.append(True)
        await asyncio.Future()
    service._execute_run = active
    with patch("app.tools.packages.shell.process_registry.kill_session_processes"):
        await service.handle_message(1, {"op": "chat.send", "turn_id": "t", "session_id": session_id, "message": "hello"})
        await service.handle_message(1, {"op": "chat.cancel", "turn_id": "t", "session_id": session_id})
    assert not started and not service._active_runs and not service._run_sessions
    if session_id:
        chat.mark_terminal_checkpoint.assert_awaited_once_with(session_id, "t", "cancelled", "cancelled")
    else:
        chat.mark_terminal_checkpoint.assert_not_awaited()
    assert service.manager.send_event.await_args.args[1].type == "done"
    assert not service._dispatch_conn
