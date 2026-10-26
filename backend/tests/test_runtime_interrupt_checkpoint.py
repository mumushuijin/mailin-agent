import copy
from types import SimpleNamespace

import aiosqlite
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt

from app.agent import graph as graph_module
from app.agent.audit import get_runtime_audit
from app.agent.nodes.tools import _interaction_request
from app.agent.state import to_graph_state, validate_agent_state
from app.agent.streaming.interrupts import has_pending_interrupt
from app.agent.recovery import recover_ledger_tail
from app.storage.checkpoint import CanonicalAsyncSqliteSaver
from app.storage.conversation_files import SessionConversationFiles

SESSION = "00000000-0000-4000-8000-000000000001"


async def run_interrupt_lifecycle(monkeypatch, tmp_path, saver):
    monkeypatch.setattr(graph_module, "get_tools", lambda: [object()])
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: saver)
    def agent(state, _config):
        message = AIMessage(content="done") if any(isinstance(m, ToolMessage) for m in state["messages"]) else AIMessage(content="", tool_calls=[{"id": "call_1", "name": "write", "args": {}}])
        return {"messages": [message], "working_messages": state["messages"], "step": int(state["step"]) + 1}
    tool_executions = []
    async def tools(state, _config):
        identity, seq, step_id = _interaction_request("approval", "call_1", prompt="approve")
        decision = interrupt({"kind": "approval", "interaction_id": identity, "interrupt_id": identity, "definition_seq": seq, "step_id": step_id, "tool_call_id": "call_1"})
        assert decision == "allow"
        tool_executions.append("call_1")
        get_runtime_audit().tool_started("call_1", 1)
        return {"messages": [ToolMessage(content="ok", tool_call_id="call_1", additional_kwargs={"tool_status": "succeeded", "attempt": 1})]}
    monkeypatch.setattr(graph_module, "call_agent", agent)
    monkeypatch.setattr(graph_module, "call_tools", tools)
    files = SessionConversationFiles(tmp_path)
    state = graph_module.make_initial_state("write", 4, session_id=SESSION, turn_id="turn_1")
    seq = files.append_messages(SESSION, "turn_1", state["context"]["working_message"], scope=state["scope"])
    state["ledger_pointer"] = files.ledger_pointer(SESSION, seq)
    config = graph_module.make_thread_config(SESSION)
    graph = graph_module.build_graph()
    await graph.ainvoke(to_graph_state(state), config)
    waiting = await graph.aget_state(config)
    validate_agent_state(waiting.values)
    assert waiting.values["turn_status"] == "waiting_user"
    assert waiting.values["current_step"]["step_name"] == "tools"
    assert "model" not in waiting.values["current_step"]
    assert waiting.values["pending_interaction"]["definition_seq"] <= waiting.values["ledger_pointer"]["applied_seq"]
    assert await has_pending_interrupt(graph, config)
    waiting_step_id = waiting.values["current_step"]["step_id"]
    started = next(row["execution"]["started_at"] for row in files.read(SESSION) if row.get("extensions", {}).get("runtime", {}).get("step_id") == waiting_step_id and row.get("execution", {}).get("status") == "running")
    assert waiting.values["current_step"]["started_at"] == started
    # Recreate the graph twice to model restart while awaiting a decision.
    for _ in range(2):
        graph = graph_module.build_graph()
        recovered = recover_ledger_tail(dict((await graph.aget_state(config)).values), files)
        assert recovered["pending_interaction"]["id"] == waiting.values["pending_interaction"]["id"]
        assert await has_pending_interrupt(graph, config)
    pending = waiting.values["pending_interaction"]
    seq, _ = files.append(SESSION, "decision_1", {"role": "user", "content": "allow", "tool_call_id": "call_1"}, turn_id="turn_1", interaction={"id": pending["id"], "kind": "approval", "decision": "allow"})
    recovered = recover_ledger_tail(dict(waiting.values), files)
    result = await graph.ainvoke(Command(resume="allow", update=to_graph_state(recovered)), config)
    assert result["turn_status"] == "completed"
    assert tool_executions == ["call_1"]
    tool_steps = [row for row in files.read(SESSION) if row.get("extensions", {}).get("runtime", {}).get("node") == "tools"]
    assert {row["extensions"]["runtime"]["step_id"] for row in tool_steps} == {waiting_step_id}
    assert len([row for row in files.read(SESSION) if row.get("interaction", {}).get("id") == pending["id"] and row["message"]["role"] == "system"]) == 1


@pytest.mark.asyncio
async def test_memory_checkpoint_preserves_native_interrupt_and_resume(monkeypatch, tmp_path):
    await run_interrupt_lifecycle(monkeypatch, tmp_path, MemorySaver())


@pytest.mark.asyncio
async def test_sqlite_checkpoint_preserves_native_interrupt_and_resume(monkeypatch, tmp_path):
    async with aiosqlite.connect(str(tmp_path / "checkpoint.sqlite")) as connection:
        saver = CanonicalAsyncSqliteSaver(connection)
        await run_interrupt_lifecycle(monkeypatch, tmp_path, saver)
