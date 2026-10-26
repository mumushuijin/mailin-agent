from __future__ import annotations

import copy

import pytest

from app.storage.ledger_contract import LEDGER_SCHEMA_VERSION, validate_event

SID = "00000000-0000-4000-8000-000000000001"


def envelope(event_type="message.accepted", payload=None, **overrides):
    value = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "session_id": SID,
        "event_seq": 1,
        "event_id": "evt_1",
        "recorded_at": "2026-10-24T00:00:01.000Z",
        "occurred_at": "2026-10-24T00:00:00.000Z",
        "branch_id": "main",
        "turn_id": "turn_1",
        "event_type": event_type,
        "payload": payload if payload is not None else {
            "submission_id": "submission_1",
            "message_id": "message_1",
            "timestamp": 1792800000000,
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        },
    }
    value.update(overrides)
    return value


def test_versioned_event_envelope_requires_identity_and_rejects_unknown_versions():
    assert validate_event(envelope())["event_seq"] == 1
    for field in ("session_id", "event_id", "branch_id", "event_type"):
        invalid = envelope()
        del invalid[field]
        with pytest.raises(ValueError):
            validate_event(invalid)
    with pytest.raises(ValueError, match="schema version"):
        validate_event({**envelope(), "schema_version": LEDGER_SCHEMA_VERSION + 1})
    with pytest.raises(ValueError, match="event type"):
        validate_event({**envelope(), "event_type": "runtime.unknown"})


def test_event_type_payloads_require_their_own_stable_identities():
    with pytest.raises(ValueError, match="submission_id"):
        validate_event(envelope(payload={"message": {"role": "user", "content": "x"}}))
    with pytest.raises(ValueError, match="assistant response"):
        validate_event(envelope("model.response.completed", {"message": {"role": "user", "content": "x"}}))
    with pytest.raises(ValueError, match="request_id"):
        validate_event(envelope("model.request.started", {}, request_id=None))
    with pytest.raises(ValueError, match="call_id"):
        validate_event(envelope("tool.attempt.started", {"execution": {"attempt": 1}}, attempt_id="attempt_1"))
    with pytest.raises(ValueError, match="interaction_id"):
        validate_event(envelope("interaction.opened", {}))
    with pytest.raises(ValueError, match="parent event cursor"):
        validate_event(envelope("branch.forked", {"parent_branch_id": "main", "parent_cursor": {}}))


def test_event_specific_facts_reject_missing_context_and_memory_provenance():
    with pytest.raises(ValueError, match="source_event_cursor"):
        validate_event(envelope("context.window.materialized", {
            "window_schema_version": "v1", "compression_version": "v1", "content_sha256": "hash",
            "bootstrap_fingerprint": "bootstrap", "reference_fingerprint": "references",
        }))
    with pytest.raises(ValueError, match="recorded_count"):
        validate_event(envelope("memory.sync.recorded", {"last_synced_turn": "turn_1", "source_files": []}))


def test_event_payloads_reject_hidden_reasoning_fields():
    invalid = copy.deepcopy(envelope())
    invalid["payload"]["message"]["reasoning_content"] = "private"
    with pytest.raises(ValueError, match="hidden reasoning"):
        validate_event(invalid)


@pytest.mark.parametrize("event_type,field", [
    ("task.started", "task_id"), ("task.settled", "task_id"),
    ("step.started", "step_id"), ("step.settled", "step_id"),
    ("activity.started", "activity_id"), ("activity.settled", "activity_id"),
])
def test_native_lifecycle_payloads_require_their_domain_identity(event_type, field):
    with pytest.raises(ValueError, match=field):
        validate_event(envelope(event_type, {}))


@pytest.mark.parametrize("event_type", ["tool.attempt.started", "tool.attempt.settled"])
def test_native_tool_attempt_requires_explicit_identity_or_execution_attempt(event_type):
    with pytest.raises(ValueError, match="attempt_id"):
        validate_event(envelope(event_type, {}, call_id="call_1"))


@pytest.mark.parametrize("event_type", ["tool.attempt.started", "tool.attempt.settled"])
@pytest.mark.parametrize("attempt", [None, 0, -1, "1", True, False, 1.0])
def test_native_tool_attempt_rejects_invalid_execution_attempt_fallback(event_type, attempt):
    with pytest.raises(ValueError, match="execution.attempt"):
        validate_event(envelope(event_type, {"execution": {"attempt": attempt}}, call_id="call_1"))


@pytest.mark.parametrize("event_type", ["tool.attempt.started", "tool.attempt.settled"])
def test_native_tool_attempt_accepts_explicit_identity_and_positive_execution_fallback(event_type):
    explicit = envelope(event_type, {}, call_id="call_1", attempt_id="attempt_1")
    assert validate_event(explicit) == explicit
    fallback = envelope(event_type, {"execution": {"attempt": 1}}, call_id="call_1")
    assert validate_event(fallback) == fallback


@pytest.mark.parametrize("event_type", ["tool.attempt.started", "tool.attempt.settled"])
def test_message_projection_tool_events_keep_missing_attempt_compatibility(event_type):
    started = event_type.endswith("started")
    message = {"role": "system", "content": [{"type": "lifecycle", "subject": "tool"}], "tool_call_id": "call_1"} if started else {
        "role": "tool", "content": "completed", "tool_call_id": "call_1",
    }
    value = envelope(event_type, {
        "message_id": "tool_event_1", "timestamp": 1792800000000, "message": message,
        "execution": {"status": "running" if started else "succeeded"},
    }, call_id="call_1")
    assert validate_event(value) == value


@pytest.mark.parametrize("event_type,payload,overrides", [
    ("message.accepted", {"submission_id": "submission_1", "message": {"role": "user", "content": "hi"}}, {}),
    ("model.response.completed", {"message": {"role": "assistant", "content": "hi"}}, {"request_id": "request_1"}),
    ("tool.attempt.settled", {"message": {"role": "tool", "content": "done", "tool_call_id": "call_1"}}, {"call_id": "call_1", "attempt_id": "attempt_1"}),
])
def test_partial_message_projection_cannot_bypass_row_validation(event_type, payload, overrides):
    with pytest.raises(ValueError, match="projection"):
        validate_event(envelope(event_type, payload, **overrides))
