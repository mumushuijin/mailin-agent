from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.context.compressor import (
    apply_layer_b_old_tool_oneline,
    build_reference_block,
    sanitize_working_messages,
)


def test_reference_block_is_human_message_not_system():
    ref = build_reference_block(
        skills_catalog="## 技能目录\n- foo: bar",
        memory_hints="hint",
        tools_disclosure="tools",
    )
    assert ref is not None
    assert isinstance(ref, HumanMessage)
    assert ref.additional_kwargs.get("context_reference") is True
    assert "非指令" in str(ref.content)


def test_layer_b_replaces_old_tool_with_oneline_summary():
    old_human = HumanMessage(content="旧问题")
    old_ai = AIMessage(
        content="",
        tool_calls=[{"id": "old-c1", "name": "read_file", "args": {"file_path": "a.txt"}}],
    )
    old_tool = ToolMessage(content="x" * 5000, tool_call_id="old-c1", name="read_file")
    old_answer = AIMessage(content="旧回答")

    new_human = HumanMessage(content="新问题")
    new_ai = AIMessage(content="回答")

    messages = [old_human, old_ai, old_tool, old_answer, new_human, new_ai]
    middle = messages[:-2]
    cleaned = apply_layer_b_old_tool_oneline(middle)

    tool_msgs = [m for m in cleaned if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert "[历史工具结果]" in str(tool_msgs[0].content)
    assert "a.txt" in str(tool_msgs[0].content)
    assert len(str(tool_msgs[0].content)) < 500


def test_layer_b_deduplicates_identical_tool_results():
    ai1 = AIMessage(content="", tool_calls=[{"id": "c1", "name": "tool", "args": {}}])
    tool1 = ToolMessage(content="same output", tool_call_id="c1", name="tool")
    ai2 = AIMessage(content="", tool_calls=[{"id": "c2", "name": "tool", "args": {}}])
    tool2 = ToolMessage(content="same output", tool_call_id="c2", name="tool")

    cleaned = apply_layer_b_old_tool_oneline([ai1, tool1, ai2, tool2])
    texts = [str(m.content) for m in cleaned if isinstance(m, ToolMessage)]
    assert any("[历史工具结果]" in t for t in texts)
    assert any("[重复工具结果]" in t for t in texts)


def test_sanitize_converts_orphan_tool_message():
    orphan = ToolMessage(content="orphan result", tool_call_id="missing", id="tool-x")
    human = HumanMessage(content="hi")

    result = sanitize_working_messages([human, orphan])

    assert not any(isinstance(m, ToolMessage) for m in result)
    assert any(isinstance(m, AIMessage) and "orphan result" in str(m.content) for m in result)


def test_sanitize_keeps_pending_tool_calls():
    human = HumanMessage(content="排盘")
    ai = AIMessage(
        content="好，来排盘了",
        tool_calls=[{"id": "c1", "name": "mcp_Bazi_MCP_getBaziDetail", "args": {}}],
    )
    result = sanitize_working_messages([human, ai])
    assert any(isinstance(m, AIMessage) and m.tool_calls for m in result)


def test_sanitize_keeps_valid_tool_chain():
    ai = AIMessage(
        content="",
        id="ai-1",
        tool_calls=[{"id": "call-1", "name": "read_file", "args": {"file_path": "a.txt"}}],
    )
    tool = ToolMessage(content="file content here", tool_call_id="call-1", id="tool-1", name="read_file")
    human = HumanMessage(content="latest question", id="human-2")
    ai2 = AIMessage(content="answer", id="ai-2")

    messages = [human, ai, tool, human, ai2]
    cleaned = sanitize_working_messages(messages)

    for i, msg in enumerate(cleaned):
        if isinstance(msg, ToolMessage):
            prev = cleaned[i - 1] if i > 0 else None
            assert isinstance(prev, AIMessage) and prev.tool_calls
            ids = {tc.get("id") for tc in prev.tool_calls}
            assert msg.tool_call_id in ids
