import json

from app.agent.streaming.events import AgentEvent, to_sse, to_ws


def test_agent_event_serializes_canonical_state_identity():
    state = {
        "checkpoint_id": "cp-1", "state_revision": 1,
        "scope": {k: f"{k}-1" for k in ("workspace_id", "session_id", "run_id", "task_id", "request_id", "step_id")},
        "context": {"working_message": []}, "max_step_every_run": 4,
        "tasks": ["task-1"], "current_task": "task-1",
        "current_step": {"step_id": "step-1", "step_name": "agent", "step_status": "running", "tool_calls": [], "token_info": {}},
        "memory": {"recorded_count": 0, "last_synced_run": None, "source_files": []},
    }
    first = AgentEvent("step_start", {"session_id": "s1"}, "run-1", state=state)
    second = AgentEvent("step_finish", {"session_id": "s1"}, "run-1", state={**state, "state_revision": 2})
    state["scope"]["run_id"] = "run-1"
    first_ws, second_ws = to_ws(first), to_ws(second)
    assert first_ws["state"]["scope"]["run_id"] == "run-1"
    assert second_ws["state"]["state_revision"] == 2
    assert second_ws["seq"] == first_ws["seq"] + 1
    payload = json.loads(to_sse(first).splitlines()[1][len("data: ") :])
    assert payload["state"]["checkpoint_id"] == "cp-1"
