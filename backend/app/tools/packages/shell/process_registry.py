from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_by_session: dict[str, set[int]] = {}


def register(session_id: str | None, proc: subprocess.Popen) -> None:
    if not session_id or proc.pid is None:
        return
    with _lock:
        _by_session.setdefault(session_id, set()).add(proc.pid)


def unregister(session_id: str | None, proc: subprocess.Popen) -> None:
    if not session_id or proc.pid is None:
        return
    with _lock:
        pids = _by_session.get(session_id)
        if pids:
            pids.discard(proc.pid)
            if not pids:
                _by_session.pop(session_id, None)


def kill_session_processes(session_id: str | None) -> int:
    """终止某会话下仍在运行的 shell 子进程，返回 kill 数量。"""
    if not session_id:
        return 0
    with _lock:
        pids = list(_by_session.pop(session_id, set()))

    killed = 0
    for pid in pids:
        if _kill_pid_tree(pid):
            killed += 1
    if killed:
        logger.info("[shell] 已终止 session=%s 下 %d 个 shell 进程", session_id, killed)
    return killed


def _kill_pid_tree(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False
