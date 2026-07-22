from __future__ import annotations

from typing import Any

from app.agent.streaming.events import AgentEvent
from app.services.ws_manager import get_ws_manager


async def push_maintenance(
    kind: str,
    message: str,
    *,
    status: str = "done",
    success: bool = True,
    session_id: str | None = None,
    task_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """推送维护任务事件（与聊天 run 解耦）。"""
    data: dict[str, Any] = {
        "kind": kind,
        "status": status,
        "message": message,
        "success": success,
    }
    if task_id:
        data["task_id"] = task_id
    if detail:
        data["detail"] = detail

    event = AgentEvent(type="background", data=data)
    mgr = get_ws_manager()
    if session_id:
        await mgr.broadcast_session(session_id, event)
    else:
        await mgr.broadcast_all(event)


async def push_background(session_id: str, kind: str, message: str, **extra) -> None:
    """兼容旧调用：会话级后台通知。"""
    await push_maintenance(
        kind,
        message,
        session_id=session_id,
        status=extra.pop("status", "done"),
        success=extra.pop("success", True),
        task_id=extra.pop("task_id", None),
        detail=extra or None,
    )


async def push_config_updated(name: str) -> None:
    """配置变更后广播给所有连接。"""
    mgr = get_ws_manager()
    await mgr.broadcast_all(
        AgentEvent(type="config_updated", data={"name": name}),
    )
