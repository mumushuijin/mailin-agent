import json

from app.agent.streaming.events import AgentEvent, to_sse, to_ws


def test_agent_event_serializes_canonical_state_identity():
    state = {
        "schema_version": 3, "checkpoint_id": "cp_" + "1" * 32, "state_revision": 1, "turn_status": "running",
        "scope": {k: f"{k}-1" for k in ("workspace_id", "session_id", "turn_id")},
        "context": {"working_message": []}, "max_step_every_turn": 4,
        "tasks": ["task-1"], "current_task": "task-1",
        "current_step": {"step_id": "step-1", "step_name": "agent", "step_status": "running", "tool_calls": []},
        "memory": {"recorded_count": 0, "last_synced_turn": None, "source_files": []},
        "ledger_pointer": {"applied_seq": 0, "event_cursor": {"branch_id": "main", "event_seq": 0, "event_id": "origin"}},
    }
    first = AgentEvent("step_start", {"session_id": "s1"}, "run-1", state=state)
    second = AgentEvent("step_finish", {"session_id": "s1"}, "run-1", state={**state, "state_revision": 2})
    state["scope"]["turn_id"] = "run-1"
    first_ws = to_ws(first, transport_id="connection-1", transport_seq=1)
    second_ws = to_ws(second, transport_id="connection-1", transport_seq=2)
    assert first_ws["state"]["scope"]["turn_id"] == "run-1"
    assert second_ws["state"]["state_revision"] == 2
    assert second_ws["seq"] == first_ws["seq"] + 1
    assert first_ws["stream_schema_version"] == second_ws["stream_schema_version"] == 3
    assert "event_seq" not in first_ws and "event_id" not in first_ws
    payload = json.loads(to_sse(first).splitlines()[1][len("data: ") :])
    assert payload["state"]["checkpoint_id"] == "cp_" + "1" * 32
    assert payload["stream_schema_version"] == 3
