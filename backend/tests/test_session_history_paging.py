from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.session import ChatMessage, Session, SessionHistory, SessionHistoryPage, ToolResultPayload


def test_history_endpoint_uses_paged_contract_when_limit_is_present(monkeypatch):
    calls: list[tuple[str, str, object]] = []
    session = Session(id="sess-1", created_at=1, updated_at=2, workspace_path="/tmp/project")

    class FakeChatService:
        async def get_history(self, session_id: str):
            calls.append(("full", session_id, None))
            return SessionHistory(session_id=session_id, messages=[ChatMessage(role="user", content="legacy")])

        async def get_history_page(self, session_id: str, *, limit: int, before: str | None = None):
            calls.append(("page", session_id, (limit, before)))
            return SessionHistoryPage(
                session_id=session_id,
                session=session,
                messages=[ChatMessage(role="user", content="newest")],
                limit=limit,
                before=before,
                has_more=True,
                next_cursor="9",
                todos=[{"id": "t1", "content": "todo", "status": "pending"}],
            )

        async def get_tool_result(self, session_id: str, tool_call_id: str):
            return ToolResultPayload(session_id=session_id, tool_call_id=tool_call_id, content="full")

    from app.api import session as session_api

    monkeypatch.setattr(session_api, "chat_service", FakeChatService())
    with TestClient(app) as client:
        legacy = client.get("/api/session/sess-1/history")
        paged = client.get("/api/session/sess-1/history?limit=1&before=10")
        full = client.get("/api/session/sess-1/history?limit=1&full=true")
        tool = client.get("/api/session/sess-1/tool-result/call-1")

    assert legacy.status_code == 200
    assert legacy.json()["messages"][0]["content"] == "legacy"
    assert paged.status_code == 200
    payload = paged.json()
    assert payload["messages"][0]["content"] == "newest"
    assert payload["session"]["id"] == "sess-1"
    assert payload["limit"] == 1
    assert payload["before"] == "10"
    assert payload["has_more"] is True
    assert payload["next_cursor"] == "9"
    assert full.json()["messages"][0]["content"] == "legacy"
    assert tool.json()["content"] == "full"
    assert calls == [
        ("full", "sess-1", None),
        ("page", "sess-1", (1, "10")),
        ("full", "sess-1", None),
    ]
