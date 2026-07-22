from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.context.budget import build_usage_report, count_message_tokens, resolve_effective_tokens
from app.context.schemas import ContextBreakdown


def test_resolve_effective_tokens_api_mixed():
    state = {
        "api_usage": {"prompt_tokens": 1000, "completion_tokens": 50, "reasoning_tokens": 10},
        "last_invoke_ledger_len": 2,
    }
    human = HumanMessage(content="用户问题")
    ai = AIMessage(content="", tool_calls=[{"id": "c1", "name": "read_file", "args": {}}])
    tool = ToolMessage(content="x" * 5000, tool_call_id="c1", name="read_file")
    ledger = [human, ai, tool]
    local = 800

    effective = resolve_effective_tokens(state, ledger, local_estimate=local)
    tool_tokens = count_message_tokens(tool)

    assert effective == 1000 + 50 + 10 + tool_tokens
    assert effective > local


def test_resolve_effective_tokens_estimate_fallback():
    human = HumanMessage(content="首轮")
    ledger = [human]
    local = 120

    effective = resolve_effective_tokens(
        {},
        ledger,
        head_tokens=100,
        reserved_tokens=50,
        local_estimate=local,
    )

    assert effective == max(100 + count_message_tokens(human) + 50, local)


def test_build_usage_report_uses_effective_for_ratio():
    breakdown = ContextBreakdown(bootstrap=100, recent_turns=500, reserved=50)
    usage = build_usage_report(breakdown, 1000, effective_tokens=900)

    assert usage.total_tokens == 900
    assert usage.ratio == 0.9
    assert usage.estimated_tokens == 650
    assert usage.to_dict()["estimated_tokens"] == 650


def test_build_usage_report_omits_estimated_when_same():
    breakdown = ContextBreakdown(bootstrap=100, recent_turns=400, reserved=50)
    usage = build_usage_report(breakdown, 1000)

    assert usage.total_tokens == 550
    assert "estimated_tokens" not in usage.to_dict()
