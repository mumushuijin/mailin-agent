from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.iteration_budget import IterationBudget
from app.agent.nodes import agent as agent_node
from app.agent.state import merge_runtime_updates, new_agent_state


def _assembled():
    return SimpleNamespace(
        rejected=False,
        messages=[HumanMessage(content="tool results are ready")],
        usage=SimpleNamespace(to_dict=lambda: {}, total_tokens=0),
        context_summary="",
        compression_count=0,
        memory_turn_counter=0,
        memory_nudge_pending=False,
        compressing=False,
    )


@pytest.mark.parametrize(
    ("response", "expected_failure"),
    [
        (AIMessage(content="final answer"), False),
        (AIMessage(content=""), True),
        (AIMessage(content="text", tool_calls=[{"id": "new-call", "name": "read_file", "args": {}}]), True),
        (None, True),
    ],
)
def test_budget_finalization_invokes_model_once_without_new_tools(monkeypatch, response, expected_failure):
    invocations = []

    def invoke(_model, messages, _session_id, **kwargs):
        invocations.append((messages, kwargs))
        return response, "timeout" if response is None else None

    monkeypatch.setattr(agent_node, "_invoke_llm", invoke)
    monkeypatch.setattr(agent_node, "_build_token_usage_updates", lambda *_args: {})
    assembled = _assembled()
    updates = agent_node._call_agent_core(
        state={},
        session_id="session-1",
        model=object(),
        model_with_tools=object(),
        ledger=[],
        cache_updates=[],
        state_for_context={},
        budget=IterationBudget(used=4, max_total=4),
        at_budget_exhausted=True,
        assembled=assembled,
        invoke_start_ledger_len=0,
    )

    assert len(invocations) == 1
    assert updates["step"] == 4
    assert not updates["messages"][-1].tool_calls
    assert (updates.get("terminal_reason") == "step_budget_exhausted") is expected_failure
    if expected_failure:
        assert updates["messages"][-1].content
        state = new_agent_state(
            workspace_id="workspace",
            session_id="00000000-0000-4000-8000-000000000001",
            message=HumanMessage(content="question"),
            max_steps=4,
        )
        assert merge_runtime_updates(state, updates)["run_status"] == "failed"
    else:
        assert updates["messages"][-1].content == "final answer"
