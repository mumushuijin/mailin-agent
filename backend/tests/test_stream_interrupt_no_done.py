"""interrupt 暂停时不得发 done，否则前端会注销 callback 导致审批弹窗丢失。"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.streaming.sse_mapper import stream_graph_events
from app.agent.state import new_agent_state
from app.storage.conversation_files import SessionConversationFiles
from langchain_core.messages import HumanMessage
from types import SimpleNamespace


def _interrupt_graph(payload=None):
    def tools_node(state: dict) -> dict:
        decision = interrupt(payload or {"kind": "approval", "tool": "write_file", "reason": "need confirm"})
        return {**state, "decision": decision}

    builder = StateGraph(dict)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile(checkpointer=MemorySaver())


@pytest.mark.asyncio
async def test_stream_graph_events_skips_done_when_interrupt_pending(monkeypatch, tmp_path):
    graph = _interrupt_graph()
    sid = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("app.core.settings.get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    state = new_agent_state(workspace_id=str(tmp_path), session_id=sid, turn_id="turn_1", message=HumanMessage(content="question"))
    files = SessionConversationFiles(tmp_path)
    seq = files.append_messages(sid, "turn_1", state["context"]["working_message"], scope=state["scope"])
    state["ledger_pointer"] = {"applied_seq": seq}
    config = {"configurable": {"thread_id": sid}}
    events = []
    async for event in stream_graph_events(graph, state, config, max_steps=3, turn_id="turn_1"):
        events.append(event.type)

    assert "done" not in events
    assert "error" not in events

    from app.agent.streaming.interrupts import has_pending_interrupt

    assert await has_pending_interrupt(graph, config) is True


@pytest.mark.asyncio
async def test_interrupt_payload_redacts_credentials_and_hidden_reasoning(monkeypatch, tmp_path):
    graph = _interrupt_graph({"kind": "approval", "tool": "write_file", "args": {"password": "credential", "reasoning_content": "private chain", "file_path": "a.txt"}, "preview": {"api_key": "credential", "reasoning": "private chain"}})
    sid = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("app.core.settings.get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    state = new_agent_state(workspace_id=str(tmp_path), session_id=sid, turn_id="turn_private", message=HumanMessage(content="write"))
    files = SessionConversationFiles(tmp_path)
    files.append_messages(sid, "turn_private", state["context"]["working_message"], scope=state["scope"])
    events = [event async for event in stream_graph_events(graph, state, {"configurable": {"thread_id": sid}}, max_steps=3, turn_id="turn_private")]
    pending = next(event for event in events if event.type == "interrupt")
    assert pending.data["args"] == {"password": "***", "file_path": "a.txt"}
    assert pending.data["preview"] == {"api_key": "***"}
    assert pending.state["state_revision"] == state["state_revision"]
    assert pending.state["checkpoint_id"] == state["checkpoint_id"]
