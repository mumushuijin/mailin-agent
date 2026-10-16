import json

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent import graph as graph_module
from app.agent.audit import get_runtime_audit
from app.core.settings import Settings, init_workspace
from app.services.chat_service import ChatService


@pytest.mark.asyncio
async def test_sync_shared_stream_and_sse_share_durable_turn_lifecycle(tmp_path, monkeypatch):
    settings = Settings(runtime_root=tmp_path / "runtime", openai_api_key="test")
    init_workspace(settings)
    project = tmp_path / "project"
    project.mkdir()
    for target in ("app.core.settings.get_settings", "app.storage.project.get_settings", "app.services.chat_service.get_settings"):
        monkeypatch.setattr(target, lambda: settings)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)
    monkeypatch.setattr(graph_module, "get_tools", lambda: [object()])
    monkeypatch.setattr(graph_module, "_resolve_checkpointer", lambda: MemorySaver())
    def model(state, _config):
        audit = get_runtime_audit()
        request = f"req_{audit.turn_id}"
        audit.model_started(request)
        message = AIMessage(content="hello", id="run--provider-unchanged", additional_kwargs={"request_id": request})
        return {"messages": [message], "working_messages": state["messages"], "request_id": request, "step": state["step"] + 1}
    monkeypatch.setattr(graph_module, "call_agent", model)
    graph = graph_module.build_graph()
    monkeypatch.setattr("app.services.chat_service.get_graph", lambda: graph)
    async def memory(_message):
        pass
    monkeypatch.setattr("app.services.chat_service.prepare_session_memory", memory)
    monkeypatch.setattr("app.services.chat_service.dispatch_post_llm_call", lambda **_kwargs: None)
    monkeypatch.setattr("app.services.chat_service.dispatch_observe", lambda *_args, **_kwargs: None)
    chat = ChatService()
    chat._schedule_background = lambda *_args: None
    contracts = []
    for channel in ("sync", "shared_stream", "sse"):
        session = chat.session_store.create(str(project))
        turn = f"turn_{channel}"
        if channel == "sync":
            response = await chat.send_sync("hi", session, turn_id=turn)
            assert response.turn_id == turn and response.content == "hello"
        elif channel == "shared_stream":
            events = [event async for event in chat.iter_chat_events("hi", session, turn_id=turn)]
            assert events[-1].turn_id == turn
            assert any(event.type == "done" for event in events)
        else:
            frames = [frame async for frame in chat.send_stream("hi", session, turn_id=turn)]
            payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in frames]
            assert all(payload["turn_id"] == turn for payload in payloads)
            assert any(payload.get("content") == "hello" for payload in payloads)
        state = dict((await graph.aget_state(graph_module.make_thread_config(session))).values)
        rows = chat.conversation_files.read(session)
        assert state["turn_status"] == "completed" and state["tasks"] == []
        assert state["ledger_pointer"]["applied_seq"] == rows[-1]["seq"]
        assert {row["turn_id"] for row in rows} == {turn}
        assert next(row["message"]["id"] for row in rows if row["message"]["role"] == "assistant") == "run--provider-unchanged"
        contracts.append([(row["message"]["role"], row.get("execution", {}).get("status")) for row in rows])
    assert contracts[0] == contracts[1] == contracts[2]
