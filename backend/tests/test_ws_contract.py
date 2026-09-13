import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_ws_ping_pong(client):
    with client.websocket_connect("/api/ws/chat") as ws:
        ws.send_text(json.dumps({"op": "ping"}))
        data = ws.receive_json()
        assert data["type"] == "pong"


def test_ws_unknown_op(client):
    with client.websocket_connect("/api/ws/chat") as ws:
        ws.send_text(json.dumps({"op": "not_a_real_op"}))
        data = ws.receive_json()
        assert data["type"] == "error"
        assert "未知操作" in data["data"]["error"]


def test_ws_invalid_json(client):
    with client.websocket_connect("/api/ws/chat") as ws:
        ws.send_text("not-json")
        data = ws.receive_json()
        assert data["type"] == "error"
        assert "JSON" in data["data"]["error"]


def test_agent_event_to_ws():
    from app.agent.streaming.events import AgentEvent, to_sse, to_ws

    event = AgentEvent("chunk", {"content": "hello"}, run_id="r1")
    ws_payload = to_ws(event)
    assert ws_payload["type"] == "chunk"
    assert ws_payload["data"]["content"] == "hello"
    assert ws_payload["run_id"] == "r1"

    sse = to_sse(event)
    assert "event: chunk" in sse
    assert "hello" in sse
    assert '"run_id": "r1"' in sse


def test_stream_event_payload_accepts_lifecycle_and_terminal_fields():
    from app.schemas.chat import StreamEventPayload

    payload = StreamEventPayload(
        type="stage",
        run_id="run-1",
        session_id="sess-1",
        stage="context_prepare",
        status="started",
        elapsed_ms=12.3,
        tool_call_id="call-1",
        todos=[{"id": "t1", "content": "todo", "status": "pending"}],
        partial=True,
        cancelled=True,
    )

    assert payload.run_id == "run-1"
    assert payload.stage == "context_prepare"
    assert payload.todos and payload.todos[0]["id"] == "t1"
