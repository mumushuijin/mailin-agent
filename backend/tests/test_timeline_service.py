from __future__ import annotations

import pytest

from app.services.timeline_service import TimelineService, _encode_cursor, normalize_timeline_event
from app.storage.conversation_files import SessionConversationFiles


SID = "00000000-0000-4000-8000-000000000001"
OTHER_SID = "00000000-0000-4000-8000-000000000002"


def append_user(files, *, event_id, branch_id, turn_id, message_id, content):
    return files.append_event(
        SID,
        "message.accepted",
        {
            "submission_id": f"submission-{message_id}",
            "message_id": message_id,
            "timestamp": 1,
            "turn_id": turn_id,
            "message": {"role": "user", "content": content},
        },
        event_id=event_id,
        branch_id=branch_id,
        turn_id=turn_id,
    )


@pytest.fixture
def timeline(tmp_path):
    files = SessionConversationFiles(tmp_path)
    append_user(
        files,
        event_id="main-1",
        branch_id="main",
        turn_id="turn-main-1",
        message_id="user-main-1",
        content="main before fork",
    )
    append_user(
        files,
        event_id="sibling-1",
        branch_id="sibling",
        turn_id="turn-sibling",
        message_id="user-sibling",
        content="sibling only",
    )
    files.append_event(
        SID,
        "branch.forked",
        {
            "parent_branch_id": "main",
            "parent_cursor": {"branch_id": "main", "event_seq": 1, "event_id": "main-1"},
        },
        event_id="child-fork",
        branch_id="child",
    )
    append_user(
        files,
        event_id="child-1",
        branch_id="child",
        turn_id="turn-child",
        message_id="user-child",
        content="child continuation",
    )
    append_user(
        files,
        event_id="main-2",
        branch_id="main",
        turn_id="turn-main-2",
        message_id="user-main-2",
        content="main after fork",
    )
    return TimelineService(files)


def test_snapshot_pages_events_in_branch_lineage_order(timeline):
    first = timeline.read_page(SID, branch_id="child", limit=2)

    assert [event["event_id"] for event in first["events"]] == ["main-1", "child-fork"]
    assert first["events"][0]["payload"] == {
        "message_id": "user-main-1",
        "content": "main before fork",
        "submission_id": "submission-user-main-1",
    }
    assert "message" not in first["events"][0]["payload"]
    assert first["has_more"] is True
    assert first["as_of_event_seq"] == 4
    assert first["projection"]["branch_view"]["visible_branches"] == [
        {"branch_id": "main", "through_event_seq": 1},
        {"branch_id": "child", "through_event_seq": None},
    ]
    assert "sibling-1" not in first["projection"]["applied_events"]
    assert "main-2" not in first["projection"]["applied_events"]


def test_after_cursor_fills_visible_events_and_uses_event_order(timeline):
    first = timeline.read_page(SID, branch_id="child", limit=2)
    resumed = timeline.read_page(
        SID,
        branch_id="child",
        limit=2,
        after_cursor=first["next_cursor"],
        include_projection=False,
    )

    assert [event["event_id"] for event in resumed["events"]] == ["child-1"]
    assert resumed["has_more"] is False
    assert "projection" not in resumed
    assert resumed["as_of_event_seq"] == 4


@pytest.mark.parametrize(
    "cursor, branch_id",
    [
        (_encode_cursor(SID, "child", {"event_seq": 5, "event_id": "main-2"}), "child"),
        (_encode_cursor(SID, "main", {"event_seq": 1, "event_id": "main-1"}), "child"),
        (_encode_cursor(OTHER_SID, "child", {"event_seq": 1, "event_id": "main-1"}), "child"),
        (_encode_cursor(SID, "child", {"event_seq": 1, "event_id": "wrong-id"}), "child"),
    ],
)
def test_after_cursor_rejects_wrong_branch_session_or_event_identity(timeline, cursor, branch_id):
    with pytest.raises(ValueError, match="cursor"):
        timeline.read_page(SID, branch_id=branch_id, after_cursor=cursor)


def test_snapshot_rejects_unknown_branch(timeline):
    with pytest.raises(ValueError, match="unknown timeline branch"):
        timeline.read_page(SID, branch_id="never-created")


def test_normalizer_emits_compact_response_and_redacts_context_window_body():
    response = normalize_timeline_event({
        "schema_version": 3,
        "session_id": SID,
        "branch_id": "main",
        "event_seq": 1,
        "event_id": "response-event",
        "turn_id": "turn-1",
        "request_id": "request-1",
        "event_type": "model.response.completed",
        "payload": {
            "message_id": "assistant-1",
            "message": {"role": "assistant", "id": "response-1", "content": "Done"},
            "execution": {"status": "succeeded"},
            "extensions": {"runtime": {"private": "omit"}},
        },
    })
    context = normalize_timeline_event({
        "schema_version": 3,
        "session_id": SID,
        "branch_id": "main",
        "event_seq": 2,
        "event_id": "context-event",
        "turn_id": "turn-1",
        "event_type": "context.window.materialized",
        "payload": {
            "window_schema_version": "1",
            "compression_version": "1",
            "content_sha256": "hash",
            "source_event_cursor": {"branch_id": "main", "event_seq": 1, "event_id": "response-event"},
            "bootstrap_fingerprint": "bootstrap",
            "reference_fingerprint": "references",
            "message": {"role": "system", "content": "model-only context"},
        },
    })

    assert response["payload"] == {
        "request_id": "request-1",
        "response_id": "response-1",
        "message_id": "assistant-1",
        "content": "Done",
    }
    assert context["payload"]["content_sha256"] == "hash"
    assert "message" not in context["payload"]
