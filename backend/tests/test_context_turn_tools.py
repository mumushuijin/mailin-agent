from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.context.budget import estimate_tokens
from app.context.compressor import apply_layer_a_tail_tool_summary, apply_layer_b_old_tool_oneline
from app.context.ledger import split_tail_window


def test_split_tail_window_respects_token_budget():
    human = HumanMessage(content="hi")
    ai = AIMessage(content="answer " * 1000)
    messages = [human, ai]
    middle, tail = split_tail_window(messages, max_tail_tokens=estimate_tokens("answer " * 200))
    assert middle == [human] or len(middle) < len(messages)
    assert tail
    assert middle + tail == messages


def test_layer_a_keeps_small_tool_results():
    human = HumanMessage(content="查")
    ai = AIMessage(content="", tool_calls=[{"id": "c1", "name": "web_search", "args": {}}])
    tool = ToolMessage(content="short result", tool_call_id="c1", name="web_search")
    messages = [human, ai, tool]

    cleaned, usages = apply_layer_a_tail_tool_summary(messages, session_id="test-session")
    tool_msgs = [m for m in cleaned if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "short result"
    assert usages == []


def test_layer_a_summarizes_large_tool_in_tail():
    human = HumanMessage(content="查热榜", id="h1")
    ai = AIMessage(content="", tool_calls=[{"id": "c1", "name": "mcp_news", "args": {}}])
    tool = ToolMessage(content="x" * 20_000, tool_call_id="c1", id="t1", name="mcp_news")
    messages = [human, ai, tool]

    cleaned, _ = apply_layer_a_tail_tool_summary(messages, session_id="test-session")
    assert not any(isinstance(m, ToolMessage) and len(str(m.content)) > 5000 for m in cleaned)
    assert any(
        isinstance(m, ToolMessage) and ("落盘" in str(m.content) or "摘要" in str(m.content))
        for m in cleaned
    )


def test_layer_b_only_applies_to_middle_outside_tail():
    old_human = HumanMessage(content="旧问题")
    old_ai = AIMessage(
        content="",
        tool_calls=[{"id": "old-c1", "name": "web_search", "args": {"query": "old"}}],
    )
    old_tool = ToolMessage(content="old " * 5000, tool_call_id="old-c1", name="web_search")
    old_answer = AIMessage(content="旧回答")

    new_human = HumanMessage(content="新问题")
    new_ai = AIMessage(
        content="",
        tool_calls=[{"id": "new-c1", "name": "web_search", "args": {"query": "new"}}],
    )
    new_tool = ToolMessage(content="new result", tool_call_id="new-c1", name="web_search")

    messages = [old_human, old_ai, old_tool, old_answer, new_human, new_ai, new_tool]
    middle, tail = split_tail_window(messages, max_tail_tokens=500)
    middle_cleaned = apply_layer_b_old_tool_oneline(middle)
    tail_cleaned, _ = apply_layer_a_tail_tool_summary(tail, session_id="test-session")

    assert any(
        isinstance(m, ToolMessage) and "[历史工具结果]" in str(m.content) for m in middle_cleaned
    )
    assert any(
        isinstance(m, ToolMessage) and "new result" in str(m.content) for m in tail_cleaned
    )
