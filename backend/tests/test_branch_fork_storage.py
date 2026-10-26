from __future__ import annotations

import pytest

from app.storage.conversation_files import SessionConversationFiles
from app.storage.event_projection import project_events


SID = "00000000-0000-4000-8000-000000000001"


def append_user(files, *, branch_id, event_id, message_id, content):
    return files.append_event(
        SID,
        "message.accepted",
        {
            "submission_id": f"submission-{message_id}",
            "message_id": message_id,
            "timestamp": 1,
            "turn_id": f"turn-{message_id}",
            "message": {"role": "user", "content": content},
        },
        event_id=event_id,
        branch_id=branch_id,
        turn_id=f"turn-{message_id}",
    )


def message_ids(projection):
    return [projection["messages"][key]["message_id"] for key in projection["message_order"]]


def test_fork_branch_persists_cursor_and_child_replay_is_immutable(tmp_path):
    files = SessionConversationFiles(tmp_path)
    append_user(files, branch_id="main", event_id="main-1", message_id="user-1", content="before fork")
    initial_events = files.read_events(SID)
    initial_parent_projection = project_events(initial_events, branch_id="main")
    original_first_line = files.ledger_path(SID).read_bytes().splitlines()[0]
    cursor = {"branch_id": "main", "event_seq": 1, "event_id": "main-1"}

    fork_seq, created = files.fork_branch(SID, "child", parent_cursor=cursor)
    assert created is True
    assert files.read_events(SID)[fork_seq - 1]["payload"] == {
        "parent_branch_id": "main",
        "parent_cursor": cursor,
    }
    assert files.fork_branch(SID, "child", parent_cursor=cursor) == (fork_seq, False)

    append_user(files, branch_id="main", event_id="main-2", message_id="user-2", content="after fork")
    append_user(files, branch_id="child", event_id="child-1", message_id="child-user", content="child continuation")
    events = files.read_events(SID)
    child_projection = project_events(events, branch_id="child")
    current_parent_projection = project_events(events, branch_id="main")

    assert message_ids(initial_parent_projection) == ["user-1"]
    assert message_ids(child_projection) == ["user-1", "child-user"]
    assert message_ids(current_parent_projection) == ["user-1", "user-2"]
    assert child_projection["branch_view"]["visible_branches"] == [
        {"branch_id": "main", "through_event_seq": 1},
        {"branch_id": "child", "through_event_seq": None},
    ]
    assert events[0]["event_id"] == initial_events[0]["event_id"]
    assert files.ledger_path(SID).read_bytes().splitlines()[0] == original_first_line


def test_fork_branch_accepts_the_explicit_root_origin_cursor(tmp_path):
    files = SessionConversationFiles(tmp_path)

    fork_seq, created = files.fork_branch(
        SID,
        "empty-child",
        parent_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"},
    )

    assert created is True
    assert fork_seq == 1
    projection = project_events(files.read_events(SID), branch_id="empty-child")
    assert projection["branch_view"]["visible_branches"] == [
        {"branch_id": "main", "through_event_seq": 0},
        {"branch_id": "empty-child", "through_event_seq": None},
    ]
    assert projection["cursor"] == {
        "branch_id": "empty-child",
        "event_seq": fork_seq,
        "event_id": files.read_events(SID)[0]["event_id"],
    }


def test_nested_fork_replays_each_parent_branch_only_through_its_cursor(tmp_path):
    files = SessionConversationFiles(tmp_path)
    append_user(files, branch_id="main", event_id="main-1", message_id="user-1", content="main")
    files.fork_branch(
        SID,
        "child",
        parent_cursor={"branch_id": "main", "event_seq": 1, "event_id": "main-1"},
    )
    append_user(files, branch_id="child", event_id="child-1", message_id="child-1", content="child before")
    files.fork_branch(
        SID,
        "grandchild",
        parent_cursor={"branch_id": "child", "event_seq": 3, "event_id": "child-1"},
    )
    append_user(files, branch_id="child", event_id="child-2", message_id="child-2", content="child after")
    append_user(files, branch_id="grandchild", event_id="grandchild-1", message_id="grandchild-1", content="grandchild")

    projection = project_events(files.read_events(SID), branch_id="grandchild")

    assert message_ids(projection) == ["user-1", "child-1", "grandchild-1"]
    assert projection["branch_view"]["visible_branches"] == [
        {"branch_id": "main", "through_event_seq": 1},
        {"branch_id": "child", "through_event_seq": 3},
        {"branch_id": "grandchild", "through_event_seq": None},
    ]


@pytest.mark.parametrize(
    "branch_id,cursor,error",
    [
        ("main", {"branch_id": "main", "event_seq": 0, "event_id": "origin"}, "root branch"),
        ("child", {"branch_id": "missing", "event_seq": 0, "event_id": "origin"}, "unknown parent branch"),
        ("child", {"branch_id": "main", "event_seq": 1, "event_id": "wrong-event"}, "owned by the selected parent"),
        ("child", {"branch_id": "main", "event_seq": 2, "event_id": "future"}, "owned by the selected parent"),
        ("child", {"branch_id": "main", "event_seq": 0, "event_id": "wrong-origin"}, "origin identity"),
        ("child", {"branch_id": "main", "event_seq": True, "event_id": "main-1"}, "parent_cursor"),
    ],
)
def test_fork_branch_rejects_invalid_parent_cursors(tmp_path, branch_id, cursor, error):
    files = SessionConversationFiles(tmp_path)
    append_user(files, branch_id="main", event_id="main-1", message_id="user-1", content="main")

    with pytest.raises(ValueError, match=error):
        files.fork_branch(SID, branch_id, parent_cursor=cursor)


def test_fork_branch_rejects_sibling_owned_cursor_and_branch_redefinition(tmp_path):
    files = SessionConversationFiles(tmp_path)
    append_user(files, branch_id="main", event_id="main-1", message_id="user-1", content="main")
    files.fork_branch(
        SID,
        "sibling",
        parent_cursor={"branch_id": "main", "event_seq": 1, "event_id": "main-1"},
    )
    append_user(files, branch_id="sibling", event_id="sibling-1", message_id="sibling-user", content="sibling")

    with pytest.raises(ValueError, match="owned by the selected parent"):
        files.fork_branch(
            SID,
            "child",
            parent_cursor={"branch_id": "main", "event_seq": 3, "event_id": "sibling-1"},
        )
    with pytest.raises(ValueError, match="different parent cursor"):
        files.fork_branch(
            SID,
            "sibling",
            parent_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"},
        )


def test_fork_branch_requires_origin_cursor_to_use_origin_identity(tmp_path):
    files = SessionConversationFiles(tmp_path)

    with pytest.raises(ValueError, match="origin identity"):
        files.fork_branch(
            SID,
            "child",
            parent_cursor={"branch_id": "main", "event_seq": 0, "event_id": "some-event"},
        )
