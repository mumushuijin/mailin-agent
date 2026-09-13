import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_config_list(client):
    r = client.get("/api/config/list")
    assert r.status_code == 200
    data = r.json()
    assert "configs" in data
    assert "CONFIG" in data["configs"]


def test_agent_info(client):
    r = client.get("/api/config/agent/info")
    assert r.status_code == 200
    assert "name" in r.json()


def test_session_crud(client, tmp_path):
    r = client.post("/api/session/create", json={"workspace_path": str(tmp_path)})
    assert r.status_code == 200
    session_id = r.json()["session_id"]

    r = client.get("/api/session/list")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["sessions"]]
    assert session_id in ids

    r = client.get(f"/api/session/{session_id}")
    assert r.status_code == 200

    r = client.get(f"/api/session/{session_id}/history")
    assert r.status_code == 200
    assert r.json()["session_id"] == session_id

    r = client.delete(f"/api/session/{session_id}")
    assert r.status_code == 200


def test_duplicate_sync_chat_route_removed(client):
    r = client.post("/api/chat/send/sync", json={"message": "hi"})
    assert r.status_code == 404


def test_memory_list(client):
    r = client.get("/api/memory/list")
    assert r.status_code == 200
    data = r.json()
    assert "memories" in data
    assert "total" in data
