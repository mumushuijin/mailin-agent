import json

import pytest

from app.schemas.session import ChatMessage
from app.storage.conversation_files import SessionConversationFiles
from app.storage.history_projection import HistoryProjectionStore, project_ledger

SID = "00000000-0000-4000-8000-000000000001"


def test_projection_rebuilds_attempts_steps_interactions_and_terminal_without_audit_body(tmp_path):
    files = SessionConversationFiles(tmp_path)
    timestamp = 1791964800000

    def append(identity, message, **extras):
        return files.append(SID, identity, message, turn_id="turn1", timestamp=timestamp + files.latest_seq(SID), **extras)

    files.ensure_metadata(SID)
    append("user", {"role": "user", "content": "run test"})
    append("step", {"role": "system", "content": [{"type": "lifecycle", "subject": "step"}]}, execution={"status": "running", "started_at": timestamp}, extensions={"runtime": {"step_id": "step1", "node": "tools"}})
    append("assistant", {"role": "assistant", "id": "run--model", "tool_calls": [{"id": "call1", "name": "run_shell", "arguments": {"command": "pytest", "api_key": "SECRET"}}]}, model={"request_id": "req1", "usage": {"input_tokens": 5, "output_tokens": 2, "reasoning_tokens": 0}})
    append("approval", {"role": "system", "tool_call_id": "call1", "content": [{"type": "lifecycle", "subject": "interaction"}, {"type": "text", "text": "allow?"}]}, interaction={"id": "i1", "kind": "approval", "mode": "single", "options": ["allow", "deny"]}, execution={"status": "waiting"})
    append("decision", {"role": "user", "content": "allow"}, interaction={"id": "i1", "kind": "approval", "decision": "allow"})
    append("start1", {"role": "system", "tool_call_id": "call1", "content": [{"type": "lifecycle", "subject": "tool"}]}, execution={"status": "running", "attempt": 1, "started_at": timestamp + 5})
    append("failed1", {"role": "system", "tool_call_id": "call1", "content": [{"type": "lifecycle", "subject": "tool"}]}, execution={"status": "failed", "attempt": 1, "finished_at": timestamp + 6, "error": {"code": "nonzero_exit"}})
    append("start2", {"role": "system", "tool_call_id": "call1", "content": [{"type": "lifecycle", "subject": "tool"}]}, execution={"status": "running", "attempt": 2, "started_at": timestamp + 7})
    reference = files.write_content(SID, "tool-results/result.txt", "complete result")
    append("result", {"role": "tool", "tool_call_id": "call1", "content": [reference]}, execution={"status": "succeeded", "attempt": 2, "finished_at": timestamp + 8})
    append("step_end", {"role": "system", "content": [{"type": "lifecycle", "subject": "step"}]}, execution={"status": "completed", "finished_at": timestamp + 9}, extensions={"runtime": {"step_id": "step1", "node": "tools"}})
    append("final", {"role": "assistant", "content": "done"}, model={"request_id": "req1", "usage": {"input_tokens": 5, "output_tokens": 2}})
    append("turn_end", {"role": "system", "content": [{"type": "lifecycle", "subject": "turn"}]}, execution={"status": "completed", "finished_at": timestamp + 11}, extensions={"runtime": {"reason": "final_answer"}})
    projected = project_ledger(files.read(SID))
    assert [m["role"] for m in projected["messages"]] == ["user", "assistant", "user", "tool", "assistant"]
    assert "SECRET" not in json.dumps(projected)
    assert projected["api_usage"] == {"input_tokens": 5, "output_tokens": 2, "reasoning_tokens": 0, "total_tokens": 7}
    timeline = projected["timelines"][0]
    assert timeline["status"] == "completed" and timeline["reason_code"] == "final_answer"
    assert timeline["steps"][0]["status"] == "completed"
    tool = timeline["tools"][0]
    assert tool["definition_seq"] == 3 and tool["result_seq"] == 9 and tool["step_id"] == "step1"
    assert tool["status"] == "succeeded" and "reason_code" not in tool
    assert [item["status"] for item in tool["attempts"]] == ["failed", "succeeded"]
    assert timeline["interactions"][0]["decision"] == "allow"
    projection = HistoryProjectionStore(tmp_path)
    projection.write(SID, [ChatMessage(**m) for m in projected["messages"]], timelines=projected["timelines"], ledger_seq=files.latest_seq(SID))
    payload = projection.read(SID)
    assert payload["version"] == 3 and payload["updated_at"] > 1_000_000_000_000
    assert all("scope" not in m and "origin" not in m and all(v is not None for v in m.values()) for m in payload["messages"])
    projection.delete(SID)
    assert project_ledger(files.read(SID)) == projected


@pytest.mark.parametrize("status", ["unknown", "denied", "skipped", "failed", "cancelled"])
def test_projection_retains_distinct_tool_outcomes(tmp_path, status):
    files = SessionConversationFiles(tmp_path)
    files.append(SID, "definition", {"role": "assistant", "tool_calls": [{"id": "c", "name": "read_file", "arguments": {}}]}, turn_id="t")
    files.append(SID, "result", {"role": "tool", "tool_call_id": "c", "content": "reason"}, turn_id="t", execution={"status": status, "error": {"code": f"tool_{status}"}})
    projected = project_ledger(files.read(SID))
    assert projected["messages"][-1]["tool_status"] == status
    assert projected["timelines"][0]["tools"][0]["status"] == status
    assert projected["messages"][-1]["tool_reason_code"] == f"tool_{status}"


def test_projection_rejects_old_version_preserves_file_and_repairs_corrupt_cache(tmp_path):
    projection = HistoryProjectionStore(tmp_path)
    path = projection.path_for(SID)
    path.parent.mkdir()
    path.write_text('{"version":2,"messages":[]}')
    before = path.read_bytes()
    with pytest.raises(ValueError, match="投影格式"):
        projection.read(SID)
    assert path.read_bytes() == before
    path.write_text("broken json")
    assert projection.read(SID) is None


def test_internal_tools_and_auxiliary_model_responses_remain_hidden(tmp_path):
    files = SessionConversationFiles(tmp_path)
    files.append(SID, "definition", {"role": "assistant", "tool_calls": [{"id": "c", "name": "tool_search", "arguments": {}}]}, turn_id="t")
    files.append(SID, "result", {"role": "tool", "tool_call_id": "c", "content": "internal"}, turn_id="t", execution={"status": "succeeded"})
    files.append(SID, "aux", {"role": "assistant", "content": "internal summary", "id": "run--aux"}, turn_id="t", extensions={"runtime": {"auxiliary": True}})
    projected = project_ledger(files.read(SID))
    assert projected["messages"] == [] and projected["timelines"][0]["tools"] == []
    assert len(files.messages(SID)) == 2
