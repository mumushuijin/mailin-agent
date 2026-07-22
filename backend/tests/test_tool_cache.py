from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.context.compressor import apply_layer_a_tail_tool_summary
from app.context.tool_cache import (
    SKIP_CACHE_KWARG,
    CACHED_PREFIX,
    cache_tool_message_if_large,
    defer_cache_tool_messages,
    is_tool_results_path,
    working_content_for_tool_message,
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.context.tool_cache.get_settings",
        lambda: type("S", (), {"workspace_path": tmp_path})(),
    )
    monkeypatch.setattr(
        "app.context.budget.get_settings",
        lambda: type("S", (), {"workspace_path": tmp_path})(),
    )
    return tmp_path


def _big_content(chars: int = 20_000) -> str:
    return "x" * chars


def test_is_tool_results_path():
    assert is_tool_results_path("tool_results/sess/call_1.json")
    assert is_tool_results_path("/tool_results/sess/call_1.json")
    assert not is_tool_results_path("notes/readme.md")


def test_current_turn_large_result_not_cached(workspace: Path):
    human = HumanMessage(content="查热榜", id="h1")
    tool = ToolMessage(
        content=_big_content(),
        tool_call_id="c1",
        id="t1",
        name="mcp_news",
    )
    messages = [human, tool]

    updates = defer_cache_tool_messages(messages, "sess-1", workspace)
    assert updates == []


def test_previous_turn_large_result_cached_on_defer(workspace: Path):
    old_human = HumanMessage(content="旧问题", id="h0")
    old_tool = ToolMessage(
        content=_big_content(),
        tool_call_id="c0",
        id="t0",
        name="mcp_news",
    )
    old_answer = AIMessage(content="旧回答", id="a0")
    new_human = HumanMessage(content="新问题", id="h1")
    messages = [old_human, old_tool, old_answer, new_human]

    updates = defer_cache_tool_messages(messages, "sess-1", workspace)
    assert len(updates) == 1
    assert updates[0].id == "t0"
    assert updates[0].additional_kwargs.get("tool_cache_path")
    assert updates[0].additional_kwargs.get("tool_cache_summary")
    assert updates[0].content == old_tool.content


def test_read_tool_results_skips_cache(workspace: Path):
    old_human = HumanMessage(content="旧问题", id="h0")
    old_tool = ToolMessage(
        content=_big_content(),
        tool_call_id="c0",
        id="t0",
        name="read_file",
        additional_kwargs={SKIP_CACHE_KWARG: True},
    )
    new_human = HumanMessage(content="新问题", id="h1")
    messages = [old_human, old_tool, new_human]

    updates = defer_cache_tool_messages(messages, "sess-1", workspace)
    assert updates == []


def test_working_content_truncates_large_text():
    human = HumanMessage(content="查热榜", id="h1")
    big = _big_content()
    tool = ToolMessage(content=big, tool_call_id="c1", id="t1", name="mcp_news")
    messages = [human, tool]

    working = working_content_for_tool_message(tool, messages)
    assert working.startswith(CACHED_PREFIX)
    assert len(working) < len(big)


def test_layer_a_summarizes_large_tail_tool(workspace: Path):
    human = HumanMessage(content="查热榜", id="h1")
    big = _big_content()
    tool = ToolMessage(content=big, tool_call_id="c1", id="t1", name="mcp_news")
    messages = [human, tool]

    cleaned, _ = apply_layer_a_tail_tool_summary(messages, session_id="sess-1")
    tool_msgs = [m for m in cleaned if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert "落盘" in str(tool_msgs[0].content) or "摘要" in str(tool_msgs[0].content)
    assert len(str(tool_msgs[0].content)) < len(big)


def test_working_content_uses_cache_reference_when_cached(workspace: Path):
    human = HumanMessage(content="旧", id="h0")
    tool = ToolMessage(content=_big_content(), tool_call_id="c0", id="t0", name="mcp_news")
    new_human = HumanMessage(content="新", id="h1")
    messages = [human, tool, new_human]

    cached = cache_tool_message_if_large(tool, "sess-1", workspace)
    working = working_content_for_tool_message(cached, messages)
    assert working.startswith(CACHED_PREFIX)
    assert "tool_results/sess-1" in working
