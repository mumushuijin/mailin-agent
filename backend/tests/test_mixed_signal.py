from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.context.budget import compute_mixed_signal, count_message_tokens


def test_mixed_signal_includes_tool_results_since_last_invoke():
    state = {
        "api_usage": {"prompt_tokens": 1000, "completion_tokens": 50, "reasoning_tokens": 10},
        "last_invoke_ledger_len": 2,
    }
    human = HumanMessage(content="用户问题")
    ai = AIMessage(content="", tool_calls=[{"id": "c1", "name": "read_file", "args": {}}])
    tool = ToolMessage(content="x" * 5000, tool_call_id="c1", name="read_file")
    ledger = [human, ai, tool]

    signal = compute_mixed_signal(state, ledger)
    tool_tokens = count_message_tokens(tool)
    assert signal == 1000 + 50 + 10 + tool_tokens


def test_mixed_signal_includes_new_user_message_after_turn():
    state = {
        "api_usage": {"prompt_tokens": 5000, "completion_tokens": 200},
        "last_invoke_ledger_len": 3,
    }
    old_human = HumanMessage(content="旧问题")
    old_ai = AIMessage(content="旧回答")
    old_tool = ToolMessage(content="ignored", tool_call_id="x", name="t")
    new_human = HumanMessage(content="新问题")
    ledger = [old_human, old_ai, old_tool, new_human]

    signal = compute_mixed_signal(state, ledger)
    assert signal == 5000 + 200 + count_message_tokens(new_human)


def test_mixed_signal_falls_back_to_local_estimate_without_api():
    human = HumanMessage(content="首轮提问")
    ledger = [human]
    signal = compute_mixed_signal(ledger=ledger, state={}, head_tokens=100, reserved_tokens=50)
    assert signal == 100 + count_message_tokens(human) + 50
