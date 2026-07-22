from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.iteration_budget import DEFAULT_MAX_STEPS, IterationBudget
from app.agent.nodes.agent import _needs_grace_summary, _run_grace_summary
from app.agent.nodes.router import effective_step, should_continue
from langgraph.graph import END


def test_budget_resets_on_new_human_message():
    state = {
        "step": 10,
        "max_steps": 24,
        "messages": [HumanMessage(content="新问题")],
    }
    budget = IterationBudget.from_state(state)
    assert budget.used == 0
    assert budget.remaining == 24
    assert not budget.exhausted


def test_budget_tracks_step_during_tool_loop():
    state = {
        "step": 3,
        "max_steps": 24,
        "messages": [
            HumanMessage(content="hi"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "read_file", "args": {}}]),
            ToolMessage(content="ok", tool_call_id="c1"),
        ],
    }
    budget = IterationBudget.from_state(state)
    assert budget.used == 3
    assert budget.remaining == 21


def test_budget_exhausted_at_max_steps():
    budget = IterationBudget(used=24, max_total=24)
    assert budget.exhausted
    assert budget.remaining == 0


def test_default_max_steps():
    budget = IterationBudget.from_state({"messages": [HumanMessage(content="hi")]})
    assert budget.max_total == DEFAULT_MAX_STEPS


def test_effective_step_matches_budget_used():
    state = {"step": 2, "max_steps": 6, "messages": [HumanMessage(content="q"), AIMessage(content="a")]}
    assert effective_step(state) == 2


def test_router_routes_to_tools_when_pending_tool_calls():
    state = {
        "step": 24,
        "max_steps": 24,
        "messages": [
            HumanMessage(content="hi"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "list_directory", "args": {}}]),
        ],
    }
    assert should_continue(state) == "tools"


def test_router_ends_without_pending_tool_calls():
    state = {
        "step": 24,
        "max_steps": 24,
        "messages": [HumanMessage(content="hi"), AIMessage(content="done")],
    }
    assert should_continue(state) == END


def test_needs_grace_summary_when_tool_calls_at_limit():
    assert _needs_grace_summary(AIMessage(content="", tool_calls=[{"id": "c1", "name": "x", "args": {}}]))


def test_needs_grace_summary_when_empty_content():
    assert _needs_grace_summary(AIMessage(content=""))


def test_needs_grace_summary_false_when_has_content():
    assert not _needs_grace_summary(AIMessage(content="最终回答"))


def test_run_grace_summary_returns_fallback_on_empty(monkeypatch):
    class _Model:
        def invoke(self, _messages, config=None):
            return AIMessage(content="")

    summary_human, grace_response = _run_grace_summary(_Model(), [HumanMessage(content="hi")], None)
    assert summary_human.content
    assert "未能生成完整总结" in str(grace_response.content)


def test_run_grace_summary_returns_model_text(monkeypatch):
    class _Model:
        def invoke(self, _messages, config=None):
            return AIMessage(content="根据上文，任务已完成。")

    _summary_human, grace_response = _run_grace_summary(_Model(), [HumanMessage(content="hi")], None)
    assert grace_response.content == "根据上文，任务已完成。"
