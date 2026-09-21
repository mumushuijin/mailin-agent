"""interrupt 暂停时不得发 done，否则前端会注销 callback 导致审批弹窗丢失。"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.streaming.sse_mapper import stream_graph_events


def _interrupt_graph():
    def tools_node(state: dict) -> dict:
        decision = interrupt({"kind": "approval", "tool": "write_file", "reason": "need confirm"})
        return {**state, "decision": decision}

    builder = StateGraph(dict)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile(checkpointer=MemorySaver())


@pytest.mark.asyncio
async def test_stream_graph_events_skips_done_when_interrupt_pending():
    graph = _interrupt_graph()
    config = {"configurable": {"thread_id": "interrupt-no-done"}}
    events = []
    async for event in stream_graph_events(graph, {}, config, max_steps=3, run_id="r1"):
        events.append(event.type)

    assert "done" not in events
    assert "error" not in events

    from app.agent.streaming.interrupts import has_pending_interrupt

    assert await has_pending_interrupt(graph, config) is True
