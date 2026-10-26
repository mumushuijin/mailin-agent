from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.audit import RuntimeLedgerAudit, bind_runtime_audit
from app.agent.nodes import agent as agent_module
from app.agent.recovery import recover_ledger_tail, append_window_messages
from app.agent.state import new_agent_state, validate_agent_state, merge_agent_state, utc_now
from app.storage.conversation_files import SessionConversationFiles

SESSION = "00000000-0000-4000-8000-000000000001"


def fixture_state(tmp_path):
    files = SessionConversationFiles(tmp_path)
    state = new_agent_state(workspace_id=str(tmp_path), session_id=SESSION, turn_id="turn_test", message=HumanMessage(content="hello"))
    user = state["context"]["working_message"][0]
    files.append_messages(SESSION, "turn_test", [user], scope=state["scope"])
    state["ledger_pointer"] = files.ledger_pointer(SESSION, 1)
    return state, files


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(schema_version=1),
    lambda s: s.update(schema_version=99),
    lambda s: s.update(run_status="running"),
    lambda s: s["scope"].update(request_id="req_old"),
    lambda s: s["scope"].update(run_id="run_old"),
    lambda s: s["memory"].update(last_synced_run="run_old"),
    lambda s: s["ledger_pointer"].update(file="ledger.jsonl"),
    lambda s: s["current_step"].update(token_info={}),
    lambda s: s["current_step"].update(step_status="unknown_status"),
    lambda s: s["current_step"].update(model={"request_id": "req_1", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}),
])
def test_checkpoint_rejects_old_or_conflicting_fields(tmp_path, mutate):
    state, _ = fixture_state(tmp_path)
    mutate(state)
    with pytest.raises(ValueError):
        validate_agent_state(state)


def test_greeting_has_no_synthetic_task_and_snapshot_id_has_no_identity(tmp_path):
    state, _ = fixture_state(tmp_path)
    validate_agent_state(state)
    assert state["schema_version"] == 3
    assert state["tasks"] == [] and state["current_task"] is None
    assert "turn_test" not in state["checkpoint_id"] and SESSION not in state["checkpoint_id"]
    assert type(state["context"]["last_built_at"]) is int


def test_node_replacement_clears_request_and_explicit_current_task(tmp_path):
    state, _ = fixture_state(tmp_path)
    state["current_step"].update(step_id="step_agent", model={"request_id": "req_1"})
    state["tasks"] = ["task_1"]
    state["current_task"] = "task_1"
    updated = merge_agent_state(state, {"state_revision": 1, "current_task": None, "current_step": {"step_id": "step_tools", "step_name": "tools", "step_status": "running", "tool_calls": []}})
    assert updated["current_task"] is None
    assert "model" not in updated["current_step"]


def call_fixture(tmp_path):
    state, files = fixture_state(tmp_path)
    assistant = AIMessage(content="", id="run--framework", tool_calls=[{"id": "call_1", "name": "dangerous", "args": {"value": 5}}])
    seq = files.append_messages(SESSION, "turn_test", [assistant], scope=state["scope"])
    state["context"]["working_message"].append(assistant)
    state["current_step"] = {"step_id": "step_tool", "step_name": "tools", "step_status": "running", "tool_calls": [{"call_id": "call_1", "definition_seq": seq, "status": "pending"}]}
    state["turn_status"] = "running"
    state["ledger_pointer"] = files.ledger_pointer(SESSION, seq)
    audit = RuntimeLedgerAudit(files, state["scope"], step_id="step_tool")
    return state, files, audit


def test_unknown_settlement_and_failed_turn_are_written_once_even_from_stale_checkpoint(tmp_path):
    state, files, audit = call_fixture(tmp_path)
    audit.tool_started("call_1", 1)
    recovered = recover_ledger_tail(state, files)
    assert recovered["turn_status"] == "failed"
    assert recovered["terminal_reason"] == "execution_outcome_unknown"
    assert recovered["current_step"]["tool_calls"][0]["status"] == "unknown"
    first_seq = files.latest_seq(SESSION)
    repeated = recover_ledger_tail(state, files)
    assert files.latest_seq(SESSION) == first_seq
    assert repeated["ledger_pointer"]["applied_seq"] == first_seq
    assert len([row for row in files.read(SESSION) if row["message"]["role"] == "tool"]) == 1
    assert all(message.type != "system" for message in recovered["context"]["working_message"])


def test_durable_result_wins_over_stale_running_checkpoint(tmp_path):
    state, files, audit = call_fixture(tmp_path)
    audit.tool_started("call_1", 1)
    result = ToolMessage(content="known success", tool_call_id="call_1", additional_kwargs={"tool_status": "succeeded", "attempt": 1, "timestamp": utc_now()})
    files.append_messages(SESSION, "turn_test", [result], scope=state["scope"])
    recovered = recover_ledger_tail(state, files)
    assert recovered["turn_status"] == "running"
    assert recovered["current_step"]["tool_calls"][0]["status"] == "succeeded"
    assert len([m for m in recovered["context"]["working_message"] if isinstance(m, ToolMessage)]) == 1
    assert not any(row.get("execution", {}).get("status") == "unknown" for row in files.read(SESSION))


def test_late_result_is_evidence_and_does_not_revive_failed_turn(tmp_path):
    state, files, audit = call_fixture(tmp_path)
    audit.tool_started("call_1", 1)
    stopped = recover_ledger_tail(state, files)
    audit.tool_late_evidence("call_1", 1, "actually finished", "succeeded")
    recovered = recover_ledger_tail(stopped, files)
    assert recovered["turn_status"] == "failed"
    assert recovered["current_step"]["tool_calls"][0]["status"] == "unknown"
    assert len([m for m in recovered["context"]["working_message"] if isinstance(m, ToolMessage)]) == 1


def test_recovery_restores_durable_decision_to_existing_interaction_without_new_turn(tmp_path):
    state, files, audit = call_fixture(tmp_path)
    seq = audit.lifecycle("interaction", "waiting", call_id="call_1", interaction={"id": "int_1", "kind": "approval"})
    state["pending_interaction"] = {"id": "int_1", "kind": "approval", "call_id": "call_1", "definition_seq": seq}
    state["ledger_pointer"] = files.ledger_pointer(SESSION, seq)
    state["turn_status"] = "waiting_user"
    files.append(SESSION, "decision_1", {"role": "user", "content": "allow", "tool_call_id": "call_1"}, turn_id="turn_test", timestamp=utc_now(), interaction={"id": "int_1", "kind": "approval", "decision": "allow"})
    recovered = recover_ledger_tail(state, files)
    assert recovered["pending_interaction"]["decision"] == "allow"
    assert recovered["scope"]["turn_id"] == "turn_test"
    assert files.latest_seq(SESSION) == recovered["ledger_pointer"]["applied_seq"]


def test_repeated_vendor_ids_are_independent_window_messages(tmp_path):
    state, files = fixture_state(tmp_path)
    messages = [AIMessage(content="first", id="run--same"), AIMessage(content=[{"type": "text", "text": "second"}, {"type": "future", "payload": [1, 2]}], id="run--same")]
    files.append_messages(SESSION, "turn_test", messages, scope=state["scope"])
    recovered = recover_ledger_tail(state, files)
    assistant = [m for m in recovered["context"]["working_message"] if isinstance(m, AIMessage)]
    assert len(assistant) == 2 and all(m.id == "run--same" for m in assistant)
    assert assistant[1].content[-1] == {"type": "future", "payload": [1, 2]}
    assert len(append_window_messages(assistant, assistant)) == 2


def test_actual_failed_retry_and_success_have_distinct_audit_request_ids(tmp_path, monkeypatch):
    state, files = fixture_state(tmp_path)
    observed = []
    class Model:
        model_name = "test-model"
        def invoke(self, _messages, config):
            observed.append(config["metadata"]["request_id"])
            if len(observed) == 1:
                raise RuntimeError("known failure")
            return AIMessage(content="done", id="run--original")
    def execute(_dependency, invoke, **_kwargs):
        try:
            invoke()
        except RuntimeError:
            pass
        return SimpleNamespace(ok=True, value=invoke())
    monkeypatch.setattr(agent_module, "execute_sync", execute)
    with bind_runtime_audit(RuntimeLedgerAudit(files, state["scope"])):
        response, error = agent_module._invoke_llm(Model(), [], SESSION)
    files.append_messages(SESSION, "turn_test", [response], scope=state["scope"])
    assert error is None
    records = files.read(SESSION)[1:]
    assert [row["execution"]["status"] for row in records] == ["running", "failed", "running", "succeeded"]
    assert records[0]["model"]["request_id"] == records[1]["model"]["request_id"]
    assert records[2]["model"]["request_id"] == records[3]["model"]["request_id"]
    assert observed[0] != observed[1]
    assert records[-1]["message"]["id"] == "run--original"


@pytest.mark.asyncio
async def test_auxiliary_async_request_audits_failure_and_response_outside_window(tmp_path):
    from app.core.model_request import bind_model_request_scope, invoke_auxiliary_model_async
    state, files = fixture_state(tmp_path)
    observed = []
    class Model:
        async def ainvoke(self, _messages, config):
            observed.append(config["metadata"])
            if len(observed) == 1:
                raise RuntimeError("known failure")
            return AIMessage(content="title", id="run--native-title", usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})
    audit = RuntimeLedgerAudit(files, state["scope"])
    with bind_model_request_scope(state["scope"]), bind_runtime_audit(audit):
        with pytest.raises(RuntimeError):
            await invoke_auxiliary_model_async(Model(), [])
        response = await invoke_auxiliary_model_async(Model(), [])
    rows = files.read(SESSION)[1:]
    assert [row["execution"]["status"] for row in rows] == ["running", "failed", "running", "succeeded"]
    assert observed[0]["request_id"] != observed[1]["request_id"]
    assert rows[-1]["model"]["usage"] == {"input_tokens": 2, "output_tokens": 1}
    assert response.id == rows[-1]["message"]["id"] == "run--native-title"
    recovered = recover_ledger_tail(state, files)
    assert recovered["context"]["working_message"] == state["context"]["working_message"]


def test_tool_settlement_is_immutable_and_late_success_is_audit_only(tmp_path, monkeypatch):
    state, files, audit = call_fixture(tmp_path)
    audit.tool_started("call_1", 1)
    stopped = audit.settle_tool(ToolMessage(content="outcome unknown", tool_call_id="call_1", additional_kwargs={"tool_status": "unknown", "attempt": 1}))
    authority = RuntimeLedgerAudit(files, state["scope"]).settle_tool(ToolMessage(content="eventually succeeded", tool_call_id="call_1", additional_kwargs={"tool_status": "succeeded", "attempt": 1}))
    assert authority.content == stopped.content
    assert authority.additional_kwargs["tool_status"] == "unknown"
    finals = [row for row in files.read(SESSION) if row["message"]["role"] == "tool"]
    assert len(finals) == 1
    assert audit.tool_result(stopped) == finals[0]["seq"]
    assert files.read(SESSION)[-1]["message"]["content"][0]["detail"] == "late_result_evidence"
    original_read = files.read_tail
    def incremental_read(session_id, after_seq=0, **kwargs):
        assert after_seq > 0, "final identities must use the message index"
        return original_read(session_id, after_seq, **kwargs)
    monkeypatch.setattr(files, "read_tail", incremental_read)
    audit.settle_tool(stopped)
    terminal = audit.turn_terminal("failed", "execution_outcome_unknown")
    assert audit.turn_terminal("failed", "execution_outcome_unknown") == terminal


def test_tail_recovery_clears_current_task_after_explicit_selection_ends(tmp_path):
    state, files = fixture_state(tmp_path)
    state["tasks"] = ["task_1"]
    state["current_task"] = "task_1"
    audit = RuntimeLedgerAudit(files, state["scope"])
    audit.lifecycle("task", "completed", detail="selected", extensions={"runtime": {"task_id": None}})
    recovered = recover_ledger_tail(state, files)
    assert recovered["current_task"] is None
    assert recovered["tasks"] == ["task_1"]


def test_actual_parallel_tool_completion_is_durable_before_batch_returns(tmp_path, monkeypatch):
    import threading
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    from app.tools.card import make_card
    from app.tools import tool_search
    state, files = fixture_state(tmp_path)
    definition = AIMessage(content="", tool_calls=[{"id": "fast", "name": "read_fast", "args": {}}, {"id": "slow", "name": "read_slow", "args": {}}])
    seq = files.append_messages(SESSION, "turn_test", [definition], scope=state["scope"])
    state["context"]["working_message"].append(definition)
    state["current_step"] = {"step_id": "step_parallel", "step_name": "tools", "step_status": "running", "tool_calls": [{"call_id": call_id, "definition_seq": seq, "status": "pending"} for call_id in ("fast", "slow")]}
    state["turn_status"] = "running"
    state["ledger_pointer"] = files.ledger_pointer(SESSION, seq)
    release = threading.Event()
    slow_started = threading.Event()
    fast_settled = threading.Event()
    def slow():
        slow_started.set()
        assert release.wait(5)
        return "late reliable completion"
    cards = [make_card(package="test", name=name, handler=handler, summary=name, description=name, display_name=name, display_icon="x", concurrency="safe", source="plugin") for name, handler in (("read_fast", lambda: "durable success"), ("read_slow", slow))]
    monkeypatch.setattr(tool_search, "_cards_for_dispatch", lambda: (cards, tool_search.ToolSearchConfig(enabled=False)))
    monkeypatch.setattr("app.storage.project.bind_session_runtime", lambda _sid: None)
    audit = RuntimeLedgerAudit(files, state["scope"])
    def cache(message, _sid):
        if message.tool_call_id == "fast":
            # This callback runs only after its actual settlement was persisted.
            fast_settled.set()
        return message
    with bind_runtime_audit(audit):
        context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(context.run, lambda: tool_search.run_tool_calls(definition.tool_calls, SESSION, turn_id="turn_test", process_for_cache=cache))
        try:
            assert fast_settled.wait(5) and slow_started.wait(5)
            assert not future.done()
            # Model process interruption after one worker settled and one started.
            stopped = recover_ledger_tail(state, files)
            calls = {call["call_id"]: call for call in stopped["current_step"]["tool_calls"]}
            assert calls["fast"]["status"] == "succeeded"
            assert calls["slow"]["status"] == "unknown"
            assert stopped["turn_status"] == "failed"
        finally:
            release.set()
        results = future.result(timeout=5)
    assert [message.additional_kwargs["tool_status"] for message in results] == ["succeeded", "unknown"]
    finals = [row for row in files.read(SESSION) if row["message"]["role"] == "tool"]
    assert len(finals) == 2
    assert len([row for row in files.read(SESSION) if isinstance(row["message"].get("content"), list) and row["message"]["content"][0].get("detail") == "late_result_evidence"]) == 1


@pytest.mark.parametrize("start_already_applied", [False, True])
def test_unsettled_actual_model_request_fails_restored_turn_once(tmp_path, start_already_applied):
    state, files = fixture_state(tmp_path)
    audit = RuntimeLedgerAudit(files, state["scope"], step_id="step_model")
    audit.node("running", name="agent")
    seq = audit.model_started("req_unfinished", "test-model")
    if start_already_applied:
        state["current_step"] = {"step_id": "step_model", "step_name": "agent", "step_status": "running", "model": {"request_id": "req_unfinished"}, "tool_calls": []}
        state["turn_status"] = "running"
        state["ledger_pointer"] = files.ledger_pointer(SESSION, seq)
    stopped = recover_ledger_tail(state, files)
    assert stopped["turn_status"] == "failed"
    assert stopped["terminal_reason"] == "model_request_outcome_unknown"
    assert stopped["current_step"]["step_status"] == "failed"
    count = files.latest_seq(SESSION)
    repeated = recover_ledger_tail(state, files)
    assert repeated["ledger_pointer"]["applied_seq"] == count
    assert files.latest_seq(SESSION) == count
    unknown = [row for row in files.read(SESSION) if row.get("model", {}).get("request_id") == "req_unfinished" and row.get("execution", {}).get("status") == "unknown"]
    assert len(unknown) == 1


def test_durable_model_response_settles_request_before_recovery(tmp_path):
    state, files = fixture_state(tmp_path)
    audit = RuntimeLedgerAudit(files, state["scope"], step_id="step_model")
    audit.node("running", name="agent")
    audit.model_started("req_finished")
    files.append_messages(SESSION, "turn_test", [AIMessage(content="known answer", additional_kwargs={"request_id": "req_finished"})], scope=state["scope"])
    restored = recover_ledger_tail(state, files)
    assert restored["turn_status"] != "failed"
    assert not any(row.get("execution", {}).get("status") == "unknown" for row in files.read(SESSION))


@pytest.mark.parametrize("step_name", ["agent", "tools"])
def test_stopping_incomplete_turn_preserves_known_result_and_pairs_unstarted_calls(tmp_path, step_name):
    state, files, audit = call_fixture(tmp_path)
    state["current_step"]["step_name"] = step_name
    assistant = AIMessage(content="", tool_calls=[{"id": "never_started", "name": "write", "args": {}}])
    seq = files.append_messages(SESSION, "turn_test", [assistant], scope=state["scope"])
    state["context"]["working_message"].append(assistant)
    state["current_step"]["tool_calls"].append({"call_id": "never_started", "definition_seq": seq, "status": "pending"})
    audit.tool_started("call_1", 1)
    known = audit.settle_tool(ToolMessage(content="known success", tool_call_id="call_1", additional_kwargs={"tool_status": "succeeded", "attempt": 1}))
    stopped = recover_ledger_tail(state, files, stop_incomplete=True)
    calls = {call["call_id"]: call for call in stopped["current_step"]["tool_calls"]}
    assert stopped["turn_status"] == "failed" and stopped["terminal_reason"] == "interrupted_turn"
    assert calls["call_1"]["status"] == "succeeded" and calls["call_1"]["result_seq"] == known.additional_kwargs["ledger_seq"]
    assert calls["never_started"]["status"] == "skipped" and "attempt" not in calls["never_started"]
    tool_rows = [row for row in files.read(SESSION) if row["message"]["role"] == "tool"]
    assert len(tool_rows) == 2
    assert tool_rows[-1]["execution"]["error"]["code"] == "interrupted_before_execution"
    assert "attempt" not in tool_rows[-1]["execution"]
    count = files.latest_seq(SESSION)
    repeated = recover_ledger_tail(state, files, stop_incomplete=True)
    assert repeated["turn_status"] == "failed" and files.latest_seq(SESSION) == count
    new_turn = new_agent_state(workspace_id=str(tmp_path), session_id=SESSION, turn_id="turn_next", message=HumanMessage(content="continue"))
    assert new_turn["turn_status"] == "created" and new_turn["scope"]["turn_id"] != stopped["scope"]["turn_id"]


def test_stopping_incomplete_turn_does_not_stop_waiting_interaction(tmp_path):
    state, files, audit = call_fixture(tmp_path)
    audit.lifecycle("interaction", "waiting", call_id="call_1", interaction={"id": "int_waiting", "kind": "approval"})
    stopped = recover_ledger_tail(state, files, stop_incomplete=True)
    assert stopped["turn_status"] == "waiting_user"
    assert stopped["pending_interaction"]["id"] == "int_waiting"
    assert not any(row["message"]["role"] == "tool" for row in files.read(SESSION))
