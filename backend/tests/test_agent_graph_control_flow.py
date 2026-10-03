from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent import graph as graph_module
from app.agent.state import to_graph_state


SESSION_ID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_canonical_graph_executes_tool_then_returns_final_answer(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_agent(state, _config):
        calls.append("agent")
        if any(isinstance(message, ToolMessage) for message in state["messages"]):
            message = AIMessage(content="final answer")
        else:
            message = AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": "read_file", "args": {"file_path": "a.txt"}}],
            )
        return {
            "messages": [message],
            "working_messages": state["messages"],
            "step": int(state["step"]) + 1,
        }

    async def fake_tools(state, _config):
        calls.append("tools")
        assert state["messages"][-1].tool_calls[0]["id"] == "call-1"
        return {
            "messages": [
                ToolMessage(
                    content="contents",
                    tool_call_id="call-1",
                    name="read_file",
                    additional_kwargs={"tool_status": "completed"},
                )
            ]
        }

    monkeypatch.setattr(graph_module, "get_tools", lambda: [object()])
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    checkpointer = MemorySaver()
    monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: checkpointer)
    monkeypatch.setattr(graph_module, "call_agent", fake_agent)
    monkeypatch.setattr(graph_module, "call_tools", fake_tools)

    graph = graph_module.build_graph()
    state = graph_module.make_initial_state("question", 4, session_id=SESSION_ID)
    updates = [
        update
        async for update in graph.astream(
            to_graph_state(state), graph_module.make_thread_config(SESSION_ID), stream_mode="updates"
        )
    ]
    result = updates[-1]["agent"]

    assert calls == ["agent", "tools", "agent"]
    first_agent = updates[0]["agent"]
    assert first_agent["run_status"] == "running"
    assert first_agent["task_details"][state["current_task"]]["status"] == "running"
    assert updates[1]["tools"]["task_details"][state["current_task"]]["status"] == "running"
    assert result["run_status"] == "completed"
    assert result["task_details"][state["current_task"]]["status"] == "completed"
    tool_calls = updates[1]["tools"]["current_step"]["tool_calls"]
    assert [(call["call_id"], call["status"]) for call in tool_calls] == [("call-1", "success")]
    assert result["current_step"]["tool_calls"] == []
    assert result["context"]["working_message"][-1].content == "final answer"
    restored = await graph_module.build_graph().aget_state(graph_module.make_thread_config(SESSION_ID))
    assert [message.content for message in restored.values["context"]["working_message"]] == [
        "question", "", "contents", "final answer",
    ]


@pytest.mark.asyncio
async def test_graph_runs_one_finalization_after_last_tool_result(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_agent(state, _config):
        used = int(state["step"])
        calls.append(f"agent:{used}")
        if used >= 4:
            return {
                "messages": [AIMessage(content="bounded final answer")],
                "working_messages": state["messages"],
                "step": used,
            }
        return {
            "messages": [AIMessage(
                content="",
                tool_calls=[{"id": f"call-{used}", "name": "read_file", "args": {}}],
            )],
            "working_messages": state["messages"],
            "step": used + 1,
        }

    async def fake_tools(state, _config):
        call_id = state["messages"][-1].tool_calls[0]["id"]
        calls.append(f"tools:{call_id}")
        return {"messages": [ToolMessage(content="done", tool_call_id=call_id, name="read_file")]}

    monkeypatch.setattr(graph_module, "get_tools", lambda: [object()])
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(graph_module, "_resolve_checkpointer", MemorySaver)
    monkeypatch.setattr(graph_module, "call_agent", fake_agent)
    monkeypatch.setattr(graph_module, "call_tools", fake_tools)

    graph = graph_module.build_graph()
    state = graph_module.make_initial_state("question", 4, session_id=SESSION_ID)
    result = await graph.ainvoke(to_graph_state(state), graph_module.make_thread_config(SESSION_ID))

    assert calls == [
        "agent:0", "tools:call-0", "agent:1", "tools:call-1",
        "agent:2", "tools:call-2", "agent:3", "tools:call-3", "agent:4",
    ]
    assert result["context"]["working_message"][-1].content == "bounded final answer"
