import pytest
from langchain_core.messages import AIMessage

from app.agent.audit import RuntimeLedgerAudit, bind_runtime_audit
from app.core.model_request import bind_model_request_scope, invoke_auxiliary_model, invoke_auxiliary_model_async
from app.storage.conversation_files import SessionConversationFiles


def test_auxiliary_requests_keep_turn_but_rotate_request_id():
    observed = []

    class Model:
        def invoke(self, _messages, config):
            observed.append(config["metadata"])
            return AIMessage(content="summary")

    scope = {"workspace_id": "w", "session_id": "s", "turn_id": "turn_1"}
    with bind_model_request_scope(scope):
        first = invoke_auxiliary_model(Model(), [])
        second = invoke_auxiliary_model(Model(), [])
    assert [row["turn_id"] for row in observed] == ["turn_1", "turn_1"]
    assert all(set(row) == {"workspace_id", "session_id", "turn_id", "request_id"} for row in observed)
    assert observed[0]["request_id"] != observed[1]["request_id"]
    assert first.additional_kwargs["request_id"] == observed[0]["request_id"]
    assert second.additional_kwargs["request_id"] == observed[1]["request_id"]


def test_model_scope_rejects_application_run_and_extra_node_identity():
    with pytest.raises(ValueError):
        with bind_model_request_scope({"workspace_id": "w", "session_id": "s", "run_id": "r", "step_id": "step_1"}):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("async_call", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("failed", [False, True], ids=["response", "failure"])
async def test_auxiliary_child_requests_leave_main_unchanged(tmp_path, async_call, failed):
    session_id = "00000000-0000-4000-8000-000000000001"
    files = SessionConversationFiles(tmp_path)
    files.fork_branch(session_id, "child", parent_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"})
    main_before = files.read_events(session_id, branch_id="main")
    scope = {"workspace_id": files.workspace_id, "session_id": session_id, "turn_id": "child-title"}
    observed = []

    class Model:
        def invoke(self, _messages, config):
            observed.append(config["metadata"])
            if failed:
                raise RuntimeError("title request failed")
            return AIMessage(content="Child title", id="provider-title-response")

        async def ainvoke(self, messages, config):
            return self.invoke(messages, config)

    audit = RuntimeLedgerAudit(files, scope, branch_id="child")
    with bind_model_request_scope(scope), bind_runtime_audit(audit):
        if failed:
            with pytest.raises(RuntimeError, match="title request failed"):
                if async_call:
                    await invoke_auxiliary_model_async(Model(), [])
                else:
                    invoke_auxiliary_model(Model(), [])
        elif async_call:
            await invoke_auxiliary_model_async(Model(), [])
        else:
            invoke_auxiliary_model(Model(), [])

    events = [event for event in files.read_events(session_id) if event.get("turn_id") == "child-title"]
    assert [event["event_type"] for event in events] == [
        "model.request.started", "model.request.failed" if failed else "model.response.completed",
    ]
    assert {event["branch_id"] for event in events} == {"child"}
    assert files.read_events(session_id, branch_id="main") == main_before
    assert all(event["request_id"] == observed[0]["request_id"] for event in events)
    if not failed:
        assert events[-1]["payload"]["extensions"]["runtime"]["auxiliary"] is True
