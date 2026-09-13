"""会话绑定的项目工作区：校验、播种、运行时上下文。"""

from __future__ import annotations

from pathlib import Path

from app.core.exceptions import AppError, NotFoundError
from app.core.settings import get_settings
from app.storage.workspace import SessionStore, seed_project_agents, validate_project_workspace
from app.tools.runtime import set_tool_project, set_tool_session


def require_project_path(raw: str | Path | None) -> Path:
    project = validate_project_workspace(raw)
    seed_project_agents(project)
    return project


def session_project_path(session_id: str | None) -> Path | None:
    if not session_id:
        return None
    store = SessionStore(get_settings().workspace_path)
    try:
        meta = store.get(session_id)
    except NotFoundError:
        return None
    raw = meta.get("workspace_path")
    if not raw:
        return None
    return Path(str(raw))


def bind_session_runtime(session_id: str | None) -> Path | None:
    """按会话记录设置工具 session / 项目 cwd。缺绑定则清空项目上下文。"""
    set_tool_session(session_id)
    project = session_project_path(session_id)
    set_tool_project(project)
    return project


def require_bound_session(session_id: str | None) -> tuple[str, Path]:
    """聊天回合前必须有有效绑定；不回退到 agent home。"""
    if not session_id:
        raise AppError("请先选择项目文件夹并创建会话")
    store = SessionStore(get_settings().workspace_path)
    meta = store.get(session_id)
    raw = meta.get("workspace_path")
    if not raw:
        raise AppError("请先为此会话绑定项目文件夹")
    project = validate_project_workspace(raw)
    set_tool_session(session_id)
    set_tool_project(project)
    return session_id, project
