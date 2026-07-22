from __future__ import annotations

import queue
import threading
from contextvars import ContextVar
from typing import Any

tool_session_id: ContextVar[str | None] = ContextVar("tool_session_id", default=None)

_lock = threading.Lock()
_progress_queues: dict[str, queue.Queue] = {}


def set_tool_session(session_id: str | None) -> None:
    tool_session_id.set(session_id)


def _get_progress_queue(session_id: str) -> queue.Queue:
    with _lock:
        q = _progress_queues.get(session_id)
        if q is None:
            q = queue.Queue()
            _progress_queues[session_id] = q
        return q


def emit_tool_progress(session_id: str | None, chunk: str, *, tool: str = "run_shell") -> None:
    if not session_id or not chunk:
        return
    q = _get_progress_queue(session_id)
    try:
        q.put_nowait({"tool": tool, "chunk": chunk})
    except queue.Full:
        pass


def drain_tool_progress(session_id: str | None) -> list[dict[str, Any]]:
    if not session_id:
        return []
    with _lock:
        q = _progress_queues.get(session_id)
    if q is None:
        return []
    items: list[dict[str, Any]] = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items
