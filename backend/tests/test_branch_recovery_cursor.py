import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.audit import RuntimeLedgerAudit
from app.agent.recovery import recover_ledger_tail
from app.agent.state import new_agent_state, validate_agent_state
from app.agent.graph import make_thread_config
from app.services.chat_service import ChatService
from app.storage.conversation_files import SessionConversationFiles


SESSION = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("branch_id", ["main", "child"])
@pytest.mark.parametrize("worker_completed", [False, True])
def test_recovery_cursor_excludes_sibling_append_after_tool_settlement(tmp_path, monkeypatch, branch_id, worker_completed):
    files = SessionConversationFiles(tmp_path)
    origin = {"branch_id": "main", "event_seq": 0, "event_id": "origin"}
    if branch_id != "main":
        files.fork_branch(SESSION, branch_id, parent_cursor=origin)
    files.fork_branch(SESSION, "sibling", parent_cursor=origin)
    state = new_agent_state(workspace_id=str(tmp_path), session_id=SESSION, turn_id="recover-turn",
                            message=HumanMessage(content="run tool"))
    definition = AIMessage(content="", tool_calls=[{"id": "call", "name": "write", "args": {}}])
    state["context"]["working_message"].append(definition)
    seq = files.append_messages(SESSION, "recover-turn", state["context"]["working_message"],
                                scope=state["scope"], branch_id=branch_id)
    state["ledger_pointer"] = files.ledger_pointer(SESSION, seq, branch_id=branch_id)
    state["turn_status"] = "running"
    state["current_step"] = {"step_id": "tool-step", "step_name": "tools", "step_status": "running",
                             "tool_calls": [{"call_id": "call", "definition_seq": seq, "status": "pending"}]}
    RuntimeLedgerAudit(files, state["scope"], branch_id=branch_id).tool_started("call", 1)
    original_settle = RuntimeLedgerAudit.settle_tool
    interleaving = []

    def settle_with_sibling_append(audit, message):
        # A worker can commit success after recovery read its running attempt.
        # Recovery must retain that first result, without running the tool again.
        if worker_completed:
            original_settle(audit, ToolMessage(content="worker completed", tool_call_id="call",
                                              additional_kwargs={"tool_status": "succeeded", "attempt": 1}))
        settled = original_settle(audit, message)
        sibling_seq, _ = files.append(SESSION, "sibling-message", {"role": "user", "content": "sibling only"},
                                      turn_id="sibling-turn", branch_id="sibling")
        interleaving.append((settled.additional_kwargs["ledger_seq"], sibling_seq))
        return settled

    monkeypatch.setattr(RuntimeLedgerAudit, "settle_tool", settle_with_sibling_append)
    recovered = recover_ledger_tail(state, files, branch_id=branch_id)

    assert len(interleaving) == 1
    result_seq, sibling_seq = interleaving[0]
    assert sibling_seq > result_seq
    result = next(message for message in recovered["context"]["working_message"] if isinstance(message, ToolMessage))
    assert result.additional_kwargs["ledger_seq"] == result_seq
    assert result.additional_kwargs["tool_status"] == ("succeeded" if worker_completed else "unknown")
    visible = files.visible_events(SESSION, branch_id=branch_id)
    assert recovered["ledger_pointer"] == files.ledger_pointer(SESSION, visible[-1]["event_seq"], branch_id=branch_id)
    assert recovered["ledger_pointer"]["event_cursor"]["branch_id"] == branch_id
    assert "sibling only" not in [message.content for message in recovered["context"]["working_message"]]
    validate_agent_state(recovered)
    before_retry = files.latest_seq(SESSION)
    assert recover_ledger_tail(recovered, files, branch_id=branch_id) == recovered
    assert files.latest_seq(SESSION) == before_retry


@pytest.mark.asyncio
@pytest.mark.parametrize("branch_id", ["main", "child"])
async def test_cancel_repair_cursor_uses_own_results_when_sibling_appends_between_settlements(tmp_path, monkeypatch, branch_id):
    files = SessionConversationFiles(tmp_path)
    origin = {"branch_id": "main", "event_seq": 0, "event_id": "origin"}
    if branch_id != "main":
        files.fork_branch(SESSION, branch_id, parent_cursor=origin)
    files.fork_branch(SESSION, "sibling", parent_cursor=origin)
    state = new_agent_state(workspace_id=str(tmp_path), session_id=SESSION, turn_id="cancel-turn",
                            message=HumanMessage(content="run two tools"))
    calls = [{"id": identity, "name": "write", "args": {}} for identity in ("call-1", "call-2")]
    state["context"]["working_message"].append(AIMessage(content="", tool_calls=calls))
    seq = files.append_messages(SESSION, "cancel-turn", state["context"]["working_message"],
                                scope=state["scope"], branch_id=branch_id)
    state["ledger_pointer"] = files.ledger_pointer(SESSION, seq, branch_id=branch_id)
    state["turn_status"] = "running"
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values=state)),
                            aupdate_state=AsyncMock())
    service = ChatService.__new__(ChatService)
    service.conversation_files = files
    service._get_graph_bounded = AsyncMock(return_value=graph)
    original_settle = RuntimeLedgerAudit.settle_tool
    interleaving = []

    def settle_with_sibling_append(audit, message):
        settled = original_settle(audit, message)
        sibling_seq, _ = files.append(SESSION, f"sibling-{message.tool_call_id}",
                                      {"role": "user", "content": "sibling only"},
                                      turn_id="sibling-turn", branch_id="sibling")
        interleaving.append((settled.additional_kwargs["ledger_seq"], sibling_seq))
        return settled

    monkeypatch.setattr(RuntimeLedgerAudit, "settle_tool", settle_with_sibling_append)
    await service.repair_cancelled_checkpoint(SESSION, branch_id=branch_id)

    assert len(interleaving) == 2
    assert all(sibling_seq > result_seq for result_seq, sibling_seq in interleaving)
    config, repaired = graph.aupdate_state.await_args.args
    assert config == make_thread_config(SESSION, branch_id)
    assert repaired["ledger_pointer"] == files.ledger_pointer(SESSION, interleaving[-1][0], branch_id=branch_id)
    results = [message for message in repaired["context"]["working_message"] if isinstance(message, ToolMessage)]
    assert {message.tool_call_id for message in results} == {"call-1", "call-2"}
    assert all(message.additional_kwargs["tool_status"] == "cancelled" for message in results)
    assert files.latest_seq(SESSION) == interleaving[-1][1] > repaired["ledger_pointer"]["applied_seq"]
    validate_agent_state(repaired)
