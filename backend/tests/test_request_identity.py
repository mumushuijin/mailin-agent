from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.agent.nodes import agent as agent_module


def test_each_actual_model_attempt_receives_a_distinct_request_id(monkeypatch):
    observed = []

    class Model:
        def invoke(self, _messages, config):
            observed.append(config["metadata"]["request_id"])
            return AIMessage(content="done", id="run--vendor-message")

    def retry_once(_dependency, invoke, **_kwargs):
        invoke()
        return SimpleNamespace(ok=True, value=invoke())

    monkeypatch.setattr(agent_module, "execute_sync", retry_once)
    response, error = agent_module._invoke_llm(Model(), [], "session-1", config={"metadata": {"trace": "yes"}})
    assert error is None
    assert len(observed) == 2 and len(set(observed)) == 2
    assert response.additional_kwargs["request_id"] == observed[-1]
    assert response.id == "run--vendor-message"
