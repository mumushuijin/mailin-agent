from __future__ import annotations

import uuid
from typing import Any

TODO_STATUSES = ("pending", "in_progress", "completed")


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def format_todos(items: list[dict[str, Any]]) -> str:
    if not items:
        return "当前会话没有待办。"
    lines = []
    for item in items:
        lines.append(f"- [{item.get('status')}] {item.get('id')}: {item.get('content')}")
    return "\n".join(lines)


def apply_todo(
    items: list[dict[str, Any]],
    *,
    action: str,
    content: str = "",
    item_id: str = "",
    status: str = "",
) -> tuple[list[dict[str, Any]] | None, str]:
    """纯函数：返回 (新列表, 文案)。失败时新列表为 None，原列表不应被写入。"""
    action = (action or "").strip().lower()
    current = [dict(x) for x in items]

    if action == "list":
        return current, format_todos(current)

    if action == "add":
        text = (content or "").strip()
        if not text:
            return None, "add 需要 content。"
        st = (status or "pending").strip().lower() or "pending"
        if st not in TODO_STATUSES:
            return None, f"非法 status: {status}。允许值为 {', '.join(TODO_STATUSES)}。"
        current.append({"id": _new_id(), "content": text, "status": st})
        return current, format_todos(current)

    if action in {"update", "complete"}:
        jid = (item_id or "").strip()
        if not jid:
            return None, f"{action} 需要 item_id。"
        target = next((x for x in current if x.get("id") == jid), None)
        if target is None:
            return None, f"找不到待办: {jid}"
        if action == "complete":
            target["status"] = "completed"
        else:
            if content and content.strip():
                target["content"] = content.strip()
            if status:
                st = status.strip().lower()
                if st not in TODO_STATUSES:
                    return None, f"非法 status: {status}。允许值为 {', '.join(TODO_STATUSES)}。"
                target["status"] = st
        return current, format_todos(current)

    return None, "action 必须是 list、add、update 或 complete。"
