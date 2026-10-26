from fastapi.testclient import TestClient

from app.main import app
from app.schemas.session import Session


def test_timeline_and_events_endpoints_delegate_with_branch_cursor(monkeypatch):
    calls = []

    class FakeSessionService:
        def get(self, session_id):
            return Session(id=session_id, created_at=1, updated_at=1)

    class FakeTimelineService:
        def read_page(self, session_id, *, branch_id, limit, after_cursor, include_projection=True):
            calls.append((session_id, branch_id, limit, after_cursor, include_projection))
            result = {
                "session_id": session_id,
                "branch_id": branch_id,
                "events": [],
                "next_cursor": "opaque-next",
                "has_more": False,
            }
            if include_projection:
                result["projection"] = {"branch_view": {"branch_id": branch_id}}
            return result

    from app.api import session as session_api

    monkeypatch.setattr(session_api, "session_service", FakeSessionService())
    monkeypatch.setattr(session_api, "timeline_service", FakeTimelineService())

    with TestClient(app) as client:
        snapshot = client.get("/api/session/session-1/timeline?branch_id=child&limit=9")
        events = client.get(
            "/api/session/session-1/events?branch_id=child&limit=4&after_cursor=opaque-before"
        )

    assert snapshot.status_code == 200
    assert snapshot.json()["projection"]["branch_view"]["branch_id"] == "child"
    assert snapshot.json()["next_cursor"] == "opaque-next"
    assert events.status_code == 200
    assert "projection" not in events.json()
    assert calls == [
        ("session-1", "child", 9, None, True),
        ("session-1", "child", 4, "opaque-before", False),
    ]
