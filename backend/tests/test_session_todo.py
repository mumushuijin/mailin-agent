from __future__ import annotations

from app.tools.packages.session.todos import apply_todo


def test_todo_add_and_complete():
    items, _ = apply_todo([], action="add", content="改断言")
    assert items is not None
    assert len(items) == 1
    item_id = items[0]["id"]
    updated, msg = apply_todo(items, action="complete", item_id=item_id)
    assert updated is not None
    assert updated[0]["status"] == "completed"
    assert "completed" in msg


def test_todo_rejects_invalid_status():
    items, _ = apply_todo([], action="add", content="x")
    assert items is not None
    item_id = items[0]["id"]
    updated, msg = apply_todo(items, action="update", item_id=item_id, status="done")
    assert updated is None
    assert "非法 status" in msg
    listed, _ = apply_todo(items, action="list")
    assert listed is not None
    assert listed[0]["status"] == "pending"
