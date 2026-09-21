"""从工具参数提取规范化资源声明。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools.policy.types import ResourceClaims

_PATH_ARG_KEYS = (
    "file_path",
    "path",
    "source",
    "destination",
    "target",
    "directory",
    "dir",
    "cwd",
    "working_directory",
)


def _as_rel(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_resource_claims(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    workspace_root: Path | None = None,
) -> ResourceClaims:
    """尽力从参数中提取路径/命令声明；无法可靠解析时标记 unresolved。"""
    raw = dict(args or {})
    paths: list[str] = []
    notes: list[str] = []
    cwd: str | None = None
    command: str | None = None
    executable: str | None = None
    cmd_args: list[str] = []
    capabilities: list[str] = []
    unresolved = False

    for key in _PATH_ARG_KEYS:
        if key not in raw:
            continue
        value = _as_rel(raw.get(key))
        if not value:
            continue
        if key in {"cwd", "working_directory"}:
            cwd = value
        else:
            paths.append(value)

    if tool_name in {"run_shell", "process"}:
        command = _as_rel(raw.get("command"))
        if tool_name == "run_shell" and not command and raw.get("action") not in (None, "", "run"):
            # process 工具用 action，不一定有 command
            pass
        if command:
            parts = command.split()
            if parts:
                executable = parts[0]
                cmd_args = parts[1:]
            if any(tok in command for tok in ("|", "&&", ";", "`", "$(", ">")):
                capabilities.append("shell_metachar")
                notes.append("命令包含管道/重定向/串联等元字符")
            if bool(raw.get("background")):
                capabilities.append("background")
            cwd = cwd or _as_rel(raw.get("cwd") or raw.get("working_directory"))
        elif tool_name == "run_shell":
            unresolved = True
            notes.append("缺少 command")

    if tool_name.startswith("mcp_"):
        notes.append("external_mcp")

    # 无法把相对路径可靠落到 workspace 时仍保留声明，由 sandbox evaluator 判定
    if workspace_root is None and paths:
        notes.append("workspace_unbound")

    # 去重保序
    uniq_paths = tuple(dict.fromkeys(paths))
    return ResourceClaims(
        paths=uniq_paths,
        cwd=cwd,
        command=command,
        executable=executable,
        args=tuple(cmd_args),
        capabilities=tuple(dict.fromkeys(capabilities)),
        unresolved=unresolved,
        notes=tuple(notes),
    )
