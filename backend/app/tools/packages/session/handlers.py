from __future__ import annotations

from app.tools.packages.session.todos import apply_todo
from app.tools.runtime import tool_todos


def todo(action: str, content: str = "", item_id: str = "", status: str = "") -> str:
    """维护当前会话待办。列表存在运行时上下文，由 tools 节点写回 checkpoint。"""
    current = list(tool_todos.get() or [])
    updated, message = apply_todo(
        current,
        action=action,
        content=content,
        item_id=item_id,
        status=status,
    )
    if updated is not None:
        tool_todos.set(updated)
    return message


def ask_user(question: str, options: list[str] | None = None, allow_multiple: bool = False) -> str:
    """占位：真正的提问在 call_tools 图线程里 interrupt，不应执行到这里。"""
    del question, options, allow_multiple
    return "未获得用户回答。"
