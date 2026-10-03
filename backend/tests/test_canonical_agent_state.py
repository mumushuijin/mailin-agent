from langchain_core.messages import HumanMessage

from app.agent.state import (
    REQUIRED_TOP_LEVEL,
    make_scope,
    merge_agent_state,
    new_agent_state,
    public_state_projection,
    validate_agent_state,
    _merge_context,
    _merge_tasks,
    _merge_step,
    terminal_patch,
)
from app.services.chat_service import ChatService
from types import SimpleNamespace
import pytest


def test_canonical_state_has_fixed_shape_and_rejects_legacy_envelope():
    state = new_agent_state(
        workspace_id="workspace-1", session_id="session-1", message=HumanMessage(content="hello")
    )
    assert REQUIRED_TOP_LEVEL.issubset(state)
    validate_agent_state(state)
    try:
        validate_agent_state({"meta": {}, "session": {}, "data": {}, "task": {}})
    except ValueError as exc:
        assert "missing keys" in str(exc)
    else:
        raise AssertionError("legacy state must not validate")


def test_reducer_merges_parallel_calls_and_rejects_scope_mismatch_and_terminal_regression():
    state = new_agent_state(
        workspace_id="workspace-1", session_id="session-1", message=HumanMessage(content="hello")
    )
    base = state["current_step"]
    state["current_step"] = {
        **base,
        "tool_calls": [
            {"call_id": "call-a", "status": "running", "attempt": 1},
            {"call_id": "call-b", "status": "running", "attempt": 1},
        ],
    }
    patch = {
        "state_revision": 1,
        "scope": state["scope"],
        "current_step": {"tool_calls": [{"call_id": "call-b", "status": "success"}]},
    }
    merged = merge_agent_state(state, patch)
    calls = {call["call_id"]: call for call in merged["current_step"]["tool_calls"]}
    assert calls["call-a"]["status"] == "running"
    assert calls["call-b"]["status"] == "success"

    wrong_scope = {**patch, "state_revision": 2, "scope": make_scope(workspace_id="other", session_id="session-1")}
    assert merge_agent_state(merged, wrong_scope) == merged

    terminal = {**patch, "state_revision": 2, "run_status": "completed", "terminal_reason": "done"}
    merged = merge_agent_state(merged, terminal)
    stale = {**patch, "state_revision": 3, "run_status": "running"}
    assert merge_agent_state(merged, stale)["run_status"] == "completed"


def test_memory_sync_metadata_survives_reducer_updates_and_deduplicates_sources():
    state = new_agent_state(
        workspace_id="workspace-1", session_id="session-1", message=HumanMessage(content="hello")
    )
    merged = merge_agent_state(state, {
        "state_revision": 1,
        "scope": state["scope"],
        "memory": {"recorded_count": 4, "last_synced_run": "run-1", "source_files": ["MEMORY.md", "USER.md"]},
    })
    merged = merge_agent_state(merged, {
        "state_revision": 2,
        "scope": state["scope"],
        "memory": {"source_files": ["MEMORY.md", "MEMORY.md"]},
    })
    assert merged["memory"]["recorded_count"] == 4
    assert merged["memory"]["last_synced_run"] == "run-1"
    assert merged["memory"]["source_files"] == ["MEMORY.md"]


def test_public_projection_redacts_secrets_from_tool_summaries():
    state = new_agent_state(
        workspace_id="workspace-1", session_id="session-1", message=HumanMessage(content="hello")
    )
    state["current_step"]["tool_calls"] = [{
        "call_id": "call-1", "output_summary": "authorization: bearer-secret password=top-secret",
    }]
    projected = public_state_projection(state)
    summary = projected["current_step"]["tool_calls"][0]["output_summary"]
    assert "bearer-secret" not in summary
    assert "top-secret" not in summary


def test_reducer_allows_explicit_pending_interaction_clear():
    state = new_agent_state(
        workspace_id="workspace-1", session_id="session-1", message=HumanMessage(content="hello")
    )
    state["pending_interaction"] = {"kind": "approval", "interrupt_id": "int-1"}
    updated = merge_agent_state(state, {
        "state_revision": 1,
        "scope": state["scope"],
        "pending_interaction": None,
    })
    assert updated["pending_interaction"] is None


def test_scope_absence_is_null_and_request_can_be_cleared():
    state = new_agent_state(
        workspace_id="workspace-1", session_id="session-1", message=HumanMessage(content="hello")
    )
    assert state["scope"]["request_id"] is None
    assert state["scope"]["step_id"] is None
    state["scope"]["request_id"] = "request-1"
    state["scope"]["step_id"] = "step-1"
    updated = merge_agent_state(state, {
        "state_revision": 1,
        "scope": {"request_id": None, "step_id": "step-2"},
    })
    assert updated["scope"]["request_id"] is None
    assert updated["scope"]["step_id"] == "step-2"
    validate_agent_state(updated)


def test_terminal_transition_closes_only_active_tasks():
    state = new_agent_state(workspace_id="w", session_id="s", message=HumanMessage(content="hi"))
    old_task = "task-old"
    state["tasks"].insert(0, old_task)
    state["task_details"][old_task] = {"status": "completed"}
    patch = terminal_patch(state, "cancelled", "cancelled")
    assert patch["task_details"][old_task]["status"] == "completed"
    assert patch["task_details"][state["current_task"]]["status"] == "cancelled"
    assert patch["run_status"] == "cancelled"


def test_checkpoint_channel_reducers_reset_run_identity_but_keep_context_by_default():
    context = {"working_message": [HumanMessage(content="old", id="old")], "summary_pointer": "summaries/s1.json"}
    replaced = _merge_context(context, {"working_message": [HumanMessage(content="new", id="new")], "_replace_working_message": True})
    assert [message.content for message in replaced["working_message"]] == ["new"]
    assert replaced["summary_pointer"] == "summaries/s1.json"
    assert _merge_tasks(["old-task"], ["new-task"]) == ["old-task", "new-task"]
    assert _merge_step(
        {"step_id": "old-step", "tool_calls": [{"call_id": "old-call"}]},
        {"step_id": "new-step", "tool_calls": []},
    ) == {"step_id": "new-step", "tool_calls": []}


@pytest.mark.asyncio
async def test_old_graph_checkpoint_is_explicitly_rejected():
    class Graph:
        async def aget_state(self, _config):
            return SimpleNamespace(values={"meta": {}, "session": {}, "data": {}, "task": {}})

    with pytest.raises(RuntimeError, match="不支持旧版 Agent checkpoint"):
        await ChatService()._reject_legacy_checkpoint(Graph(), {})
