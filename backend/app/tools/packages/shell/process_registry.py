from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    session_id: str
    command: str
    pid: int
    status: str = "running"
    exit_code: int | None = None
    error: str | None = None
    proc: subprocess.Popen | None = None
    output_parts: list[str] = field(default_factory=list)
    output_lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> str:
        cmd = self.command if len(self.command) <= 80 else self.command[:77] + "..."
        extra = f" exit={self.exit_code}" if self.exit_code is not None else ""
        return f"{self.id}\t{self.status}{extra}\t{cmd}"

    def output_text(self) -> str:
        with self.output_lock:
            return "".join(self.output_parts)


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_by_session: dict[str, set[str]] = {}


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def register(session_id: str | None, proc: subprocess.Popen, command: str = "") -> Job | None:
    if not session_id or proc.pid is None:
        return None
    job = Job(
        id=_new_job_id(),
        session_id=session_id,
        command=command or "",
        pid=proc.pid,
        proc=proc,
    )
    with _lock:
        _jobs[job.id] = job
        _by_session.setdefault(session_id, set()).add(job.id)
    return job


def unregister(session_id: str | None, proc: subprocess.Popen, *, status: str = "exited", exit_code: int | None = None) -> None:
    if not session_id or proc.pid is None:
        return
    with _lock:
        for job in list(_jobs.values()):
            if job.session_id == session_id and job.pid == proc.pid:
                job.status = status
                if exit_code is not None:
                    job.exit_code = exit_code
                job.proc = None
                break


def get_job(job_id: str, session_id: str | None) -> Job | None:
    if not job_id or not session_id:
        return None
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.session_id != session_id:
            return None
        return job


def list_jobs(session_id: str | None) -> list[Job]:
    if not session_id:
        return []
    with _lock:
        ids = list(_by_session.get(session_id, set()))
        return [_jobs[jid] for jid in ids if jid in _jobs]


def append_output(job: Job, chunk: str) -> None:
    with job.output_lock:
        job.output_parts.append(chunk)


def mark_job(job: Job, *, status: str, exit_code: int | None = None, error: str | None = None) -> None:
    job.status = status
    if exit_code is not None:
        job.exit_code = exit_code
    if error is not None:
        job.error = error
    if status in {"exited", "killed"}:
        job.proc = None


def kill_job(job: Job) -> bool:
    proc = job.proc
    killed = False
    if proc is not None and proc.poll() is None:
        killed = _kill_pid_tree(proc.pid)
        try:
            proc.kill()
        except OSError:
            pass
    elif job.pid:
        killed = _kill_pid_tree(job.pid)
    mark_job(job, status="killed", error="已被终止")
    return killed


def wait_job(job: Job, timeout: int) -> tuple[str, int, str | None]:
    proc = job.proc
    if proc is None:
        code = job.exit_code if job.exit_code is not None else 0
        return job.output_text(), code, job.error

    try:
        proc.wait(timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        return job.output_text(), -1, f"等待超时（>{timeout}s），作业仍在运行"

    code = proc.returncode or 0
    mark_job(job, status="exited", exit_code=code)
    return job.output_text(), code, None


def kill_session_processes(session_id: str | None) -> int:
    """终止某会话下仍在运行的 shell 子进程，返回 kill 数量。"""
    if not session_id:
        return 0
    with _lock:
        ids = list(_by_session.get(session_id, set()))
        jobs = [_jobs[jid] for jid in ids if jid in _jobs]

    killed = 0
    for job in jobs:
        if job.status == "running" and kill_job(job):
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
