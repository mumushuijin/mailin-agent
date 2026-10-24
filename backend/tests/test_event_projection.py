from __future__ import annotations

import copy

import pytest

from app.storage.event_projection import project_events
from app.storage.ledger_contract import LEDGER_SCHEMA_VERSION

SID = "00000000-0000-4000-8000-000000000001"
STAMP = "2026-10-24T00:00:00.000Z"


def event(seq, event_id, event_type, payload, *, branch="main", turn="turn-1", **identity):
    value = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "session_id": SID,
        "event_seq": seq,
        "event_id": event_id,
        "recorded_at": STAMP,
        "occurred_at": STAMP,
        "branch_id": branch,
        "event_type": event_type,
        "payload": payload,
    }
    if turn is not None:
        value["turn_id"] = turn
    value.update(identity)
    return value


def test_projection_replays_messages_and_runtime_facts_in_event_order():
    events = [
        event(1, "e1", "message.accepted", {
            "submission_id": "submission-1", "message_id": "user-1", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "run"}]},
        }),
        event(2, "e2", "task.started", {"task_id": "task-1", "title": "Build"}),
        event(3, "e3", "step.started", {"step_id": "step-1", "node": "agent", "task_id": "task-1"}),
        event(4, "e4", "activity.started", {"activity_kind": "context", "label": "Prepare", "summary": "Preparing"}, activity_id="activity-1"),
        event(5, "e5", "model.request.started", {}, request_id="request-1"),
        event(6, "e6", "model.response.completed", {
            "message_id": "assistant-1", "timestamp": 2,
            "message": {"role": "assistant", "id": "run--model", "content": "Using a tool"},
        }, request_id="request-1"),
        event(7, "e7", "tool.attempt.started", {
            "call_id": "call-1", "attempt_id": "attempt-1", "tool_name": "read_file",
            "input_summary": "README.md",
        }, call_id="call-1", attempt_id="attempt-1", activity_id="activity-1"),
        event(8, "e8", "tool.attempt.settled", {
            "call_id": "call-1", "attempt_id": "attempt-1", "status": "failed", "reason_code": "not_found",
        }, call_id="call-1", attempt_id="attempt-1"),
        event(9, "e9", "tool.attempt.started", {
            "call_id": "call-1", "attempt_id": "attempt-2", "tool_name": "read_file",
        }, call_id="call-1", attempt_id="attempt-2"),
        event(10, "e10", "tool.attempt.settled", {
            "call_id": "call-1", "attempt_id": "attempt-2", "status": "succeeded", "result_summary": "Found file",
        }, call_id="call-1", attempt_id="attempt-2"),
        event(11, "e11", "interaction.opened", {
            "interaction_id": "interaction-1", "kind": "approval", "prompt_summary": "Continue?",
        }, interaction_id="interaction-1"),
        event(12, "e12", "interaction.resolved", {
            "interaction_id": "interaction-1", "status": "completed", "decision_summary": "Approved",
        }, interaction_id="interaction-1"),
        event(
            13, "e13", "activity.settled", {"status": "completed", "summary": "Ready"},
            activity_id="activity-1", occurred_at="2026-10-24T00:00:01.000Z",
        ),
        event(14, "e14", "step.settled", {"step_id": "step-1", "status": "completed"}),
        event(15, "e15", "task.settled", {"task_id": "task-1", "status": "completed"}),
        event(16, "e16", "model.response.completed", {
            "message_id": "assistant-final", "timestamp": 3,
            "message": {"role": "assistant", "content": "Done"},
        }, request_id="request-2"),
        event(17, "e17", "turn.settled", {
            "status": "completed", "final_message_id": "assistant-final",
        }),
    ]

    projected = project_events(events, branch_id="main")

    assert projected["turns"]["turn-1"]["status"] == "completed"
    assert projected["turns"]["turn-1"]["final_message_id"] == "assistant-final"
    assert [projected["messages"][key]["message_id"] for key in projected["message_order"]] == [
        "user-1", "assistant-1", "assistant-final",
    ]
    assert projected["messages"][projected["message_order"][1]]["raw_message_id"] == "run--model"
    request = projected["model_requests"]["[\"turn-1\",\"request-1\"]"]
    assert request["status"] == "succeeded" and request["settled_seq"] == 6
    tool = projected["tools"][projected["tool_order"][0]]
    assert tool["status"] == "succeeded"
    assert [attempt["status"] for attempt in tool["attempts"]] == ["failed", "succeeded"]
    assert tool["attempts"][0]["reason_code"] == "not_found"
    assert tool["result_summary"] == "Found file"
    activity = projected["activities"][projected["activity_order"][0]]
    assert activity["status"] == "completed" and activity["duration_ms"] == 1000
    assert projected["interactions"][projected["interaction_order"][0]]["decision_summary"] == "Approved"
    assert projected["tasks"][projected["task_order"][0]]["status"] == "completed"
    assert projected["steps"][projected["step_order"][0]]["status"] == "completed"
    assert [item["event_seq"] for item in projected["items"]] == sorted(item["event_seq"] for item in projected["items"])


def test_projection_is_idempotent_for_duplicate_delivery_and_rejects_identity_conflicts():
    accepted = event(1, "e1", "message.accepted", {
        "submission_id": "submission-1", "message_id": "user-1", "timestamp": 1,
        "message": {"role": "user", "content": "hello"},
    })
    projected = project_events([accepted, copy.deepcopy(accepted)], branch_id="main")
    assert len(projected["messages"]) == 1
    assert len(projected["applied_events"]) == 1

    conflicting = copy.deepcopy(accepted)
    conflicting["payload"]["message"]["content"] = "different"
    with pytest.raises(ValueError, match="event_id integrity conflict"):
        project_events([accepted, conflicting], branch_id="main")


def test_projection_uses_fork_cursor_and_ignores_sibling_branch_events():
    events = [
        event(1, "main-1", "message.accepted", {
            "submission_id": "submission-1", "message_id": "user-1", "timestamp": 1,
            "message": {"role": "user", "content": "first"},
        }),
        event(2, "main-2", "model.response.completed", {
            "message_id": "assistant-1", "timestamp": 2,
            "message": {"role": "assistant", "content": "answer"},
        }, request_id="request-1"),
        event(3, "sibling-1", "message.accepted", {
            "submission_id": "sibling-submission", "message_id": "sibling-user", "timestamp": 3,
            "message": {"role": "user", "content": "sibling"},
        }, branch="sibling", turn="sibling-turn"),
        event(4, "child-fork", "branch.forked", {
            "parent_branch_id": "main",
            "parent_cursor": {"branch_id": "main", "event_seq": 2, "event_id": "main-2"},
        }, branch="child", turn=None),
        event(5, "child-1", "message.accepted", {
            "submission_id": "child-submission", "message_id": "child-user", "timestamp": 5,
            "message": {"role": "user", "content": "continue"},
        }, branch="child", turn="child-turn"),
        event(6, "other-child-1", "message.accepted", {
            "submission_id": "other-submission", "message_id": "other-user", "timestamp": 6,
            "message": {"role": "user", "content": "other child"},
        }, branch="other-child", turn="other-turn"),
    ]
    projected = project_events(events, branch_id="child")

    assert [projected["messages"][key]["message_id"] for key in projected["message_order"]] == [
        "user-1", "assistant-1", "child-user",
    ]
    assert projected["branch_view"]["visible_branches"] == [
        {"branch_id": "main", "through_event_seq": 2},
        {"branch_id": "child", "through_event_seq": None},
    ]
    assert projected["cursor"] == {"branch_id": "child", "event_seq": 5, "event_id": "child-1"}
    assert "sibling-1" not in projected["applied_events"]
    assert "other-child-1" not in projected["applied_events"]


def test_projection_supports_empty_session_and_cursor_limited_replay():
    empty = project_events([], branch_id="main", session_id=SID)
    assert empty["cursor"] is None
    accepted = event(1, "e1", "message.accepted", {
        "submission_id": "submission-1", "message_id": "user-1", "timestamp": 1,
        "message": {"role": "user", "content": "hello"},
    })
    later = event(2, "e2", "turn.settled", {"status": "completed"})
    earlier = project_events([accepted, later], branch_id="main", as_of_event_seq=1)
    assert earlier["turns"]["turn-1"]["status"] == "running"
    assert earlier["cursor"]["event_seq"] == 1

