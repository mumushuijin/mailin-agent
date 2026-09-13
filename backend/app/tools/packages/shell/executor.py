from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.context.tool_cache import save_tool_result
from app.core.settings import get_settings
from app.storage.workspace import assert_not_agent_space, _resolve_safe_path, normalize_workspace_path
from app.tools.runtime import get_project_workspace
from app.tools.packages.shell.env import build_subprocess_env
from app.tools.packages.shell.process_registry import (
    Job,
    append_output,
    kill_session_processes,
    mark_job,
    register,
    unregister,
)
from app.tools.packages.shell.truncate import DEFAULT_MAX_BYTES, truncate_tail
from app.tools.runtime import emit_tool_progress, tool_session_id

logger = logging.getLogger(__name__)


def _workspace_root() -> Path:
    root = get_project_workspace()
    if root is None:
        raise ValueError("未绑定项目工作区")
    return root


def resolve_workdir(workdir: str | None, default: str = ".") -> tuple[Path | None, str | None]:
    rel = normalize_workspace_path(workdir or default or ".")
    try:
        path = _resolve_safe_path(_workspace_root(), rel)
        assert_not_agent_space(path)
    except ValueError as exc:
        return None, str(exc)
    path.mkdir(parents=True, exist_ok=True)
    return path, None


def execute_shell(
    command: str,
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str],
    session_id: str | None,
    on_line: Callable[[str], None] | None = None,
) -> tuple[str, int, str | None]:
    """执行 shell 命令，返回 (合并输出, exit_code, error_message)。"""
    creationflags = 0
    preexec_fn = None
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        preexec_fn = os.setsid

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
    except OSError as exc:
        return "", -1, f"无法启动命令: {exc}"

    job = register(session_id, proc, command)
    output_parts: list[str] = []
    timed_out = False
    lock = threading.Lock()

    def _consume_line(line: str) -> None:
        with lock:
            output_parts.append(line)
        if job is not None:
            append_output(job, line)
        if on_line:
            on_line(line)

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            _consume_line(line)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    try:
        while reader.is_alive():
            if time.monotonic() > deadline:
                timed_out = True
                kill_session_processes(session_id)
                try:
                    proc.kill()
                except OSError:
                    pass
                break
            reader.join(timeout=0.05)
        if not timed_out:
            proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        timed_out = True
    finally:
        unregister(session_id, proc, status="exited", exit_code=-1 if timed_out else (proc.returncode or 0))
        if job is not None:
            mark_job(
                job,
                status="exited",
                exit_code=-1 if timed_out else (proc.returncode or 0),
                error=f"命令超时（>{timeout}s）" if timed_out else None,
            )

    reader.join(timeout=1)
    output = "".join(output_parts)
    if timed_out:
        return output, -1, f"命令超时（>{timeout}s）"
    return output, proc.returncode or 0, None


def execute_shell_background(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str],
    session_id: str | None,
) -> tuple[Job | None, str | None]:
    """后台启动命令，立即返回作业。"""
    creationflags = 0
    preexec_fn = None
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        preexec_fn = os.setsid
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
    except OSError as exc:
        return None, f"无法启动命令: {exc}"

    job = register(session_id, proc, command)
    if job is None:
        try:
            proc.kill()
        except OSError:
            pass
        return None, "无法登记后台作业（缺少 session）。"

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            append_output(job, line)
        code = proc.wait()
        mark_job(job, status="exited", exit_code=code)

    threading.Thread(target=_reader, daemon=True).start()
    return job, None


def format_shell_result(
    command: str,
    output: str,
    exit_code: int,
    *,
    session_id: str | None,
    error: str | None = None,
) -> str:
    if error:
        body = output.strip()
        prefix = f"命令执行失败: {error}"
        return f"{prefix}\n\n{body}" if body else prefix

    truncated = truncate_tail(output)
    full_output = output
    display = truncated.content.strip()

    if truncated.total_bytes > DEFAULT_MAX_BYTES and session_id:
        try:
            path = save_tool_result(session_id, "run_shell", full_output)
            rel = path.relative_to(_workspace_root()).as_posix()
            display += f"\n\n[完整输出已落盘: {rel}，可用 read_file 分页读取]"
        except Exception:
            logger.exception("落盘 shell 输出失败")

    if exit_code != 0:
        trailer = f"\n\n命令以退出码 {exit_code} 结束"
        return (display + trailer) if display else trailer.strip()

    if not display:
        return "命令执行成功（无输出）。"
    return display


def run_with_progress(
    command: str,
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str],
) -> tuple[str, int, str | None]:
    session_id = tool_session_id.get()

    def _on_line(line: str) -> None:
        emit_tool_progress(session_id, line, tool="run_shell")

    return execute_shell(
        command,
        cwd=cwd,
        timeout=timeout,
        env=env,
        session_id=session_id,
        on_line=_on_line,
    )
