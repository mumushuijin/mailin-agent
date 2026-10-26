"""Exercise window persistence and recovery through real graph node boundaries."""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent import graph as graph_module
from app.agent.audit import RuntimeLedgerAudit
from app.agent.recovery import recover_ledger_tail
from app.agent.state import new_agent_state, to_graph_state, validate_agent_state
from app.storage.conversation_files import SessionConversationFiles
from app.storage.model_window import ModelWindowStore

SESSION = "00000000-0000-4000-8000-000000000061"
TURN = "turn_window_integration"


async def _completed_graph(monkeypatch, tmp_path, *, input_content=None):
    monkeypatch.setattr(graph_module, "get_tools", lambda: [])
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: MemorySaver())
    invocations = []

    def reply(state, _config):
        invocations.append("model")
        return {
            "messages": [AIMessage(content="durable answer", id="provider-response", additional_kwargs={"request_id": "req_graph_window"})],
            "working_messages": state["messages"],
            "step": int(state["step"]) + 1,
        }

    monkeypatch.setattr(graph_module, "call_agent", reply)
    files = SessionConversationFiles(tmp_path)
    initial = graph_module.make_initial_state("hello", 4, session_id=SESSION, turn_id=TURN)
    if input_content is not None:
        initial["context"]["working_message"][0].content = input_content
    seq = files.append_messages(SESSION, TURN, initial["context"]["working_message"], scope=initial["scope"])
    initial["ledger_pointer"] = files.ledger_pointer(SESSION, seq)
    graph = graph_module.build_graph()
    config = graph_module.make_thread_config(SESSION)
    await graph.ainvoke(to_graph_state(initial), config)
    final = dict((await graph.aget_state(config)).values)
    validate_agent_state(final)
    assert invocations == ["model"]
    return final, files, invocations


@pytest.mark.asyncio
async def test_graph_materializes_window_before_its_event_and_exports_resource(monkeypatch, tmp_path):
    state, files, _ = await _completed_graph(monkeypatch, tmp_path)
    events = files.read_events(SESSION)
    materialized = [event for event in events if event["event_type"] == "context.window.materialized"]
    assert len(materialized) == 1
    payload = materialized[0]["payload"]
    reference = state["context"]["window_pointer"]
    assert payload["window_ref"] == reference
    assert "messages" not in payload
    assert "working_message" not in payload
    window = ModelWindowStore(files).load(
        SESSION, reference,
        expected_source_event_cursor=payload["source_event_cursor"],
        expected_bootstrap_fingerprint=payload["bootstrap_fingerprint"],
        expected_reference_fingerprint=payload["reference_fingerprint"],
    )
    assert window.content_sha256 == payload["content_sha256"]
    assert [message["message"]["content"] for message in window.messages] == ["hello", "durable answer"]
    response = next(event for event in events if event["event_type"] == "model.response.completed")
    assert payload["source_event_cursor"] == {"branch_id": "main", "event_seq": response["event_seq"], "event_id": response["event_id"]}
    assert response["event_seq"] < materialized[0]["event_seq"] < state["ledger_pointer"]["applied_seq"]
    exported = files.export_files(SESSION)
    assert exported[reference["path"]] == files.read_reference(SESSION, reference)
    assert {"ledger_meta.json", "ledger.jsonl"}.issubset(exported)


@pytest.mark.asyncio
async def test_control_event_advances_checkpoint_without_changing_window(monkeypatch, tmp_path):
    state, files, invocations = await _completed_graph(monkeypatch, tmp_path)
    reference = copy.deepcopy(state["context"]["window_pointer"])
    before = files.read_reference(SESSION, reference)
    messages = copy.deepcopy(state["context"]["working_message"])
    seq, _ = files.append_event(
        SESSION, "memory.sync.recorded",
        {"recorded_count": 1, "last_synced_turn": TURN, "source_files": ["bootstraps/MEMORY.md"]},
        turn_id=TURN,
    )

    recovered = recover_ledger_tail(state, files)

    assert recovered["ledger_pointer"]["event_cursor"] == files.ledger_pointer(SESSION, seq)["event_cursor"]
    assert recovered["state_revision"] > state["state_revision"]
    assert recovered["context"]["window_pointer"] == reference
    assert files.read_reference(SESSION, reference) == before
    assert recovered["context"]["working_message"] == messages
    assert recovered["memory"]["recorded_count"] == 1
    assert invocations == ["model"]


@pytest.mark.asyncio
async def test_new_model_event_invalidates_window_and_repairs_messages_once(monkeypatch, tmp_path):
    state, files, invocations = await _completed_graph(monkeypatch, tmp_path)
    old_reference = copy.deepcopy(state["context"]["window_pointer"])
    seq = files.append_messages(SESSION, TURN, [
        AIMessage(content="later committed response", additional_kwargs={"request_id": "req_after_window"}),
    ], scope=state["scope"])

    recovered = recover_ledger_tail(state, files)

    assert recovered["context"]["window_pointer"] is None
    assert recovered["context"]["last_built_at"] is None
    assert recovered["ledger_pointer"]["applied_seq"] == seq
    assert [message.content for message in recovered["context"]["working_message"]].count("later committed response") == 1
    repeated = recover_ledger_tail(recovered, files)
    assert repeated["context"]["working_message"] == recovered["context"]["working_message"]
    assert repeated["turn_status"] == "completed"
    assert files.read_reference(SESSION, old_reference)
    assert invocations == ["model"]


@pytest.mark.asyncio
async def test_bootstrap_change_invalidates_existing_window_without_calling_model(monkeypatch, tmp_path):
    state, files, invocations = await _completed_graph(monkeypatch, tmp_path)
    state["context"]["bootstrap_fingerprint"] = hashlib.sha256(b"updated bootstrap").hexdigest()
    before = files.latest_seq(SESSION)

    recovered = recover_ledger_tail(state, files)

    assert recovered["context"]["window_pointer"] is None
    assert recovered["context"]["last_built_at"] is None
    assert files.latest_seq(SESSION) == before
    assert invocations == ["model"]


@pytest.mark.parametrize("change_reference", [False, True])
@pytest.mark.asyncio
async def test_checkpoint_window_reuse_requires_current_reference_fingerprint(monkeypatch, tmp_path, change_reference):
    resources = SessionConversationFiles(tmp_path)
    reference = resources.write_content(SESSION, "content/window-input.txt", "original context")
    state, files, invocations = await _completed_graph(monkeypatch, tmp_path, input_content=[reference])
    window_reference = copy.deepcopy(state["context"]["window_pointer"])
    original_cursor = copy.deepcopy(state["ledger_pointer"])
    if change_reference:
        replacement = files.write_content(SESSION, "content/window-input-updated.txt", "updated context")
        state["context"]["working_message"][0].content = [replacement]
    before_ledger = files.ledger_path(SESSION).read_bytes()

    recovered = recover_ledger_tail(state, files)

    assert recovered["context"]["window_pointer"] == (None if change_reference else window_reference)
    if change_reference:
        assert recovered["context"]["last_built_at"] is None
    assert recovered["ledger_pointer"] == original_cursor
    assert files.ledger_path(SESSION).read_bytes() == before_ledger
    assert invocations == ["model"]


@pytest.mark.parametrize("checkpoint_includes_event", [False, True])
@pytest.mark.asyncio
async def test_native_window_materialization_is_applied_only_after_checkpoint_cursor(monkeypatch, tmp_path, checkpoint_includes_event):
    state, files, invocations = await _completed_graph(monkeypatch, tmp_path)
    reference = copy.deepcopy(state["context"]["window_pointer"])
    graph_event = next(event for event in files.read_events(SESSION) if event["event_type"] == "context.window.materialized")
    payload = {key: copy.deepcopy(graph_event["payload"][key]) for key in (
        "window_ref", "window_schema_version", "compression_version", "content_sha256",
        "source_event_cursor", "bootstrap_fingerprint", "reference_fingerprint",
    )}
    seq, _ = files.append_event(SESSION, "context.window.materialized", payload, turn_id=TURN)
    assert "message" not in payload
    state["context"]["window_pointer"] = None
    if checkpoint_includes_event:
        state["ledger_pointer"] = files.ledger_pointer(SESSION, seq)
    messages = copy.deepcopy(state["context"]["working_message"])
    before_ledger = files.ledger_path(SESSION).read_bytes()

    recovered = recover_ledger_tail(state, files)

    assert recovered["context"]["window_pointer"] == (None if checkpoint_includes_event else reference)
    assert recovered["ledger_pointer"] == files.ledger_pointer(SESSION, seq)
    assert recovered["context"]["working_message"] == messages
    assert files.ledger_path(SESSION).read_bytes() == before_ledger
    assert invocations == ["model"]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.asyncio
async def test_missing_or_corrupt_graph_window_fails_recovery_with_diagnostic(monkeypatch, tmp_path, damage):
    state, files, invocations = await _completed_graph(monkeypatch, tmp_path)
    reference = state["context"]["window_pointer"]
    resource = files.session_dir(SESSION) / reference["path"]
    before_ledger = files.ledger_path(SESSION).read_bytes()
    if damage == "missing":
        resource.unlink()
    else:
        resource.write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="unavailable|integrity"):
        recover_ledger_tail(state, files)

    assert files.ledger_path(SESSION).read_bytes() == before_ledger
    assert invocations == ["model"]


@pytest.mark.asyncio
async def test_window_hash_mismatch_is_rejected_before_materialization_commit(monkeypatch, tmp_path):
    state, files, _ = await _completed_graph(monkeypatch, tmp_path)
    event = next(event for event in files.read_events(SESSION) if event["event_type"] == "context.window.materialized")
    context = copy.deepcopy(event["payload"]["extensions"]["context"])
    context["content_sha256"] = "0" * 64
    before = files.ledger_path(SESSION).read_bytes()

    with pytest.raises(ValueError, match="content hash does not match"):
        RuntimeLedgerAudit(files, state["scope"]).lifecycle("context", "completed", extensions={"context": context})

    assert files.ledger_path(SESSION).read_bytes() == before


@pytest.mark.asyncio
async def test_checkpoint_window_reference_mismatch_fails_recovery(monkeypatch, tmp_path):
    state, files, invocations = await _completed_graph(monkeypatch, tmp_path)
    state["context"]["window_pointer"]["bytes"] += 1
    before = files.ledger_path(SESSION).read_bytes()

    with pytest.raises(ValueError, match="reference integrity"):
        recover_ledger_tail(state, files)

    assert files.ledger_path(SESSION).read_bytes() == before
    assert invocations == ["model"]


@pytest.mark.parametrize("damage", ["different-id", "missing-cursor"])
@pytest.mark.asyncio
async def test_checkpoint_cursor_identity_is_validated_before_recovery(monkeypatch, tmp_path, damage):
    state, files, invocations = await _completed_graph(monkeypatch, tmp_path)
    if damage == "different-id":
        state["ledger_pointer"]["event_cursor"]["event_id"] = "evt_wrong_identity"
    else:
        state["ledger_pointer"].pop("event_cursor")
    original = copy.deepcopy(state)
    before_ledger = files.ledger_path(SESSION).read_bytes()

    with pytest.raises(ValueError, match="cursor"):
        recover_ledger_tail(state, files)

    assert state == original
    assert files.ledger_path(SESSION).read_bytes() == before_ledger
    assert invocations == ["model"]


@pytest.mark.asyncio
async def test_missing_checkpoint_rebuilds_completed_graph_from_committed_facts(monkeypatch, tmp_path):
    original, files, invocations = await _completed_graph(monkeypatch, tmp_path)
    before = files.ledger_path(SESSION).read_bytes()

    rebuilt = recover_ledger_tail({}, files, session_id=SESSION)
    repeated = recover_ledger_tail({}, files, session_id=SESSION)

    validate_agent_state(rebuilt)
    assert rebuilt["scope"]["turn_id"] == TURN
    assert rebuilt["turn_status"] == "completed"
    assert rebuilt["ledger_pointer"]["event_cursor"] == original["ledger_pointer"]["event_cursor"]
    assert rebuilt["context"]["window_pointer"] == original["context"]["window_pointer"]
    assert [message.content for message in rebuilt["context"]["working_message"]] == ["hello", "durable answer"]
    assert repeated["context"]["working_message"] == rebuilt["context"]["working_message"]
    assert files.ledger_path(SESSION).read_bytes() == before
    assert invocations == ["model"]


def test_missing_checkpoint_settles_unfinished_request_without_reexecution(tmp_path):
    files = SessionConversationFiles(tmp_path)
    initial = new_agent_state(workspace_id=str(tmp_path), session_id=SESSION, turn_id=TURN, message=HumanMessage(content="hello"))
    files.append_messages(SESSION, TURN, initial["context"]["working_message"], scope=initial["scope"])
    audit = RuntimeLedgerAudit(files, initial["scope"], step_id="step_unfinished")
    audit.node("running", name="agent")
    audit.model_started("req_unfinished_window")

    rebuilt = recover_ledger_tail({}, files, session_id=SESSION)
    count = files.latest_seq(SESSION)
    repeated = recover_ledger_tail({}, files, session_id=SESSION)

    validate_agent_state(rebuilt)
    assert rebuilt["turn_status"] == "failed"
    assert rebuilt["terminal_reason"] == "model_request_outcome_unknown"
    assert not any(event["event_type"] == "model.response.completed" for event in files.read_events(SESSION))
    interrupted = [event for event in files.read_events(SESSION) if event["event_type"] == "model.request.interrupted"]
    assert len(interrupted) == 1
    assert repeated["turn_status"] == "failed"
    assert files.latest_seq(SESSION) == count
