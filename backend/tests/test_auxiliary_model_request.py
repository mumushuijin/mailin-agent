from langchain_core.messages import AIMessage

from app.core.model_request import bind_model_request_scope, invoke_auxiliary_model


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
    import pytest
    with pytest.raises(ValueError):
        with bind_model_request_scope({"workspace_id": "w", "session_id": "s", "run_id": "r", "step_id": "step_1"}):
            pass
