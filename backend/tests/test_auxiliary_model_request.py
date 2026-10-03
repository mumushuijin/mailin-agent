from langchain_core.messages import AIMessage

from app.core.model_request import bind_model_request_scope, invoke_auxiliary_model


def test_auxiliary_requests_keep_run_and_step_but_rotate_request_id():
    observed = []

    class Model:
        def invoke(self, _messages, config):
            observed.append(config["metadata"])
            return AIMessage(content="summary")

    scope = {"workspace_id": "w", "session_id": "s", "run_id": "r", "task_id": "t",
             "step_id": "step-1", "request_id": None}
    with bind_model_request_scope(scope):
        first = invoke_auxiliary_model(Model(), [])
        second = invoke_auxiliary_model(Model(), [])
    assert [row["run_id"] for row in observed] == ["r", "r"]
    assert [row["step_id"] for row in observed] == ["step-1", "step-1"]
    assert observed[0]["request_id"] != observed[1]["request_id"]
    assert first.additional_kwargs["request_id"] == observed[0]["request_id"]
    assert second.additional_kwargs["request_id"] == observed[1]["request_id"]
