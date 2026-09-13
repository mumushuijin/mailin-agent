from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from app.core.latency import (
    LATENCY_STAGE_ACCEPTED,
    LATENCY_STAGE_CONTEXT_PREPARE,
    LATENCY_STAGE_MODEL_STREAM,
    RunLatency,
)
from app.core.settings import Settings, init_workspace
from app.services.chat_service import ChatService
from app.storage.workspace import SessionStore


@pytest.fixture
def latency_spaces(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = Settings(
        workspace_path=home,
        workspace_defaults_path=Path(__file__).resolve().parents[1] / "workspace_defaults",
        openai_api_key="test-key",
    )
    init_workspace(settings)
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.storage.project.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)
    return settings, home, project


def test_run_latency_summary_contains_stage_durations():
    latency = RunLatency(run_id="run-1", session_id="sess-1")
    started = latency.start(LATENCY_STAGE_CONTEXT_PREPARE)
    assert started["run_id"] == "run-1"
    assert started["session_id"] == "sess-1"
    assert started["status"] == "started"

    finished = latency.finish(LATENCY_STAGE_CONTEXT_PREPARE)
    assert finished["duration_ms"] >= 0
    summary = latency.summary()
    assert summary["run_id"] == "run-1"
    assert summary["session_id"] == "sess-1"
    assert summary["total_duration_ms"] >= 0
    assert LATENCY_STAGE_CONTEXT_PREPARE in summary["stage_durations_ms"]


@pytest.mark.asyncio
async def test_iter_chat_events_acknowledges_before_expensive_graph_work(latency_spaces, monkeypatch, caplog):
    settings, home, project = latency_spaces

    memory_called = asyncio.Event()
    graph_called = asyncio.Event()

    async def fake_memory(_message: str) -> None:
        memory_called.set()

    class DummyGraph:
        async def astream_events(self, _state, _config, version: str):
            graph_called.set()
            yield {"event": "on_chain_start", "name": "agent", "data": {}}
            yield {"event": "on_chat_model_stream", "name": "model", "data": {"chunk": AIMessageChunk(content="hi")}}
            yield {"event": "on_chain_end", "name": "agent", "data": {"output": {"api_usage": {"total_tokens": 3}}}}

        async def aget_state(self, _config):
            return SimpleNamespace(values={"messages": [AIMessage(content="hi")], "api_usage": {"total_tokens": 3}})

    monkeypatch.setattr("app.services.chat_service.prepare_session_memory", fake_memory)
    monkeypatch.setattr("app.services.chat_service.get_graph", lambda: DummyGraph())
    monkeypatch.setattr("app.services.chat_service.dispatch_post_llm_call", lambda **_kwargs: None)
    monkeypatch.setattr("app.services.chat_service.dispatch_observe", lambda *_args, **_kwargs: None)

    chat = ChatService()
    chat.settings = settings
    chat.session_store = SessionStore(home)
    chat._schedule_background = lambda _name, _factory: None  # type: ignore[method-assign]
    sid = chat.session_store.create(str(project))

    events = []
    async for event in chat.iter_chat_events("hello", sid, run_id="run-1"):
        events.append(event)
        if len(events) == 2:
            assert not memory_called.is_set()
            assert not graph_called.is_set()

    assert [e.type for e in events[:4]] == ["session", "stage", "stage", "stage"]
    assert events[1].data["stage"] == LATENCY_STAGE_ACCEPTED
    assert events[2].data["stage"] == LATENCY_STAGE_CONTEXT_PREPARE
    assert events[2].data["status"] == "started"
    assert events[3].data["stage"] == LATENCY_STAGE_CONTEXT_PREPARE
    assert any(e.type == "stage" and e.data["stage"] == LATENCY_STAGE_MODEL_STREAM for e in events)
    assert any(e.type == "done" and e.data["session_id"] == sid for e in events)
    records = [r for r in caplog.records if r.message == "chat.run.completed"]
    assert records
    assert getattr(records[-1], "run_id") == "run-1"
    assert getattr(records[-1], "session_id") == sid
    assert getattr(records[-1], "total_duration_ms") >= 0


@pytest.mark.asyncio
async def test_memory_preparation_timeout_degrades_without_hanging(latency_spaces, monkeypatch):
    settings, home, _project = latency_spaces

    async def slow_memory(_message: str) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr("app.services.chat_service.prepare_session_memory", slow_memory)
    monkeypatch.setattr("app.services.chat_service.PREPARE_MEMORY_TIMEOUT_SECONDS", 0.01)

    chat = ChatService()
    chat.settings = settings
    chat.session_store = SessionStore(home)
    status = await chat._prepare_memory_bounded("hello", RunLatency("run-1", "sess-1"))
    assert status == "degraded"
