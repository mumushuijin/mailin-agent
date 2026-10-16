import json
import os
import sys
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent import graph as graph_module
from app.agent.audit import RuntimeLedgerAudit, bind_runtime_audit
from app.agent.recovery import recover_ledger_tail
from app.agent.state import new_agent_state, to_graph_state
from app.storage.conversation_files import SessionConversationFiles
from app.tools import tool_search
from app.tools.card import make_card
from app.tools.packages.shell.executor import execute_shell, format_shell_result
from app.tools.runtime import set_tool_project

SESSION = "00000000-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_actual_large_shell_result_roundtrips_resource_and_bounded_checkpoint(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    set_tool_project(project)
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(workspace_path=home))
    monkeypatch.setattr(graph_module, "get_tools", lambda: [object()])
    monkeypatch.setattr(graph_module, "_resolve_checkpointer", MemorySaver)
    monkeypatch.setattr("app.storage.project.bind_session_runtime", lambda _sid: project)
    source = "head_marker\n" + "line abcdefghijklmnopqrstuvwxyz\n" * 4000 + "tail_marker\n"
    script = project / "output.py"
    script.write_text("import sys\nsys.stdout.write(" + repr(source) + ")", encoding="utf-8")
    def handler():
        output, code, error = execute_shell(f'"{sys.executable}" "{script}"', cwd=project, timeout=20, env=dict(os.environ), session_id=SESSION)
        assert error is None and code == 0
        return format_shell_result("test-output", output, code, session_id=SESSION)
    card = make_card(package="shell", name="run_shell", handler=handler, summary="test", description="test", display_name="test", display_icon="x", concurrency="barrier", source="plugin")
    monkeypatch.setattr(tool_search, "execute_single_tool_call", lambda *_args, **_kwargs: ("run_shell", handler()))
    def agent(state, _config):
        message = AIMessage(content="finished") if any(isinstance(msg, ToolMessage) for msg in state["messages"]) else AIMessage(content="", tool_calls=[{"id": "call_large", "name": "run_shell", "args": {}}])
        return {"messages": [message], "working_messages": state["messages"], "step": state["step"] + 1}
    async def tools(state, _config):
        from app.agent.audit import get_runtime_audit
        get_runtime_audit().tool_started("call_large", 1)
        message = tool_search._execute_parsed_call("run_shell", {}, "call_large", cards=[card], config=tool_search.ToolSearchConfig(enabled=False), session_id=SESSION, process_for_cache=lambda msg, _sid: msg)
        assert "head_marker" in message.content and len(message.content) > 64 * 1024
        message.additional_kwargs["attempt"] = 1
        return {"messages": [message]}
    monkeypatch.setattr(graph_module, "call_agent", agent)
    monkeypatch.setattr(graph_module, "call_tools", tools)
    files = SessionConversationFiles(home)
    state = graph_module.make_initial_state("emit large result", 4, session_id=SESSION)
    state["ledger_pointer"]["applied_seq"] = files.append_messages(SESSION, state["scope"]["turn_id"], state["context"]["working_message"], scope=state["scope"])
    graph = graph_module.build_graph()
    result = await graph.ainvoke(to_graph_state(state), graph_module.make_thread_config(SESSION))
    row = next(row for row in files.read(SESSION) if row["message"]["role"] == "tool")
    reference = row["message"]["content"][0]
    assert reference["type"] == "reference"
    restored = files.decode_row(row, session_id=SESSION, resolve_references=True)
    assert restored.content.replace("\r\n", "\n") == source
    checkpoint_tool = next(msg for msg in result["context"]["working_message"] if isinstance(msg, ToolMessage))
    assert len(checkpoint_tool.content) < 1000
    assert checkpoint_tool.additional_kwargs["ledger_reference"]["sha256"] == reference["sha256"]
    stale = new_agent_state(workspace_id=str(home), session_id=SESSION, turn_id=state["scope"]["turn_id"], message=HumanMessage(content="emit large result"))
    recovered = recover_ledger_tail(stale, files)
    assert len(next(msg for msg in recovered["context"]["working_message"] if isinstance(msg, ToolMessage)).content) < 1000
    assert not (project / ".mailin" / "tool_results" / SESSION / "run_shell.json").exists()


def test_mcp_result_preserves_nontext_content_and_full_text():
    from app.tools.mcp.handlers import _format_tool_result
    from mcp.types import TextContent, ImageContent
    text = "x" * 100_000
    result = SimpleNamespace(content=[TextContent(type="text", text=text), ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")], structuredContent={"value": 8}, isError=False)
    payload = json.loads(_format_tool_result(result))
    assert payload["result"] == text
    assert payload["content"][0]["data"] == "aGVsbG8="
    assert payload["structuredContent"] == {"value": 8}
