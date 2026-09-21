from __future__ import annotations

from app.tools.packages.shell.config import load_shell_config
from app.tools.packages.shell.executor import (
    execute_shell_background,
    format_shell_result,
    resolve_workdir,
    run_with_progress,
)
from app.tools.packages.shell.env import build_subprocess_env
from app.tools.packages.shell.process_registry import get_job, kill_job, list_jobs
from app.tools.runtime import tool_session_id


def run_shell(
    command: str,
    timeout: int | None = None,
    workdir: str | None = None,
    background: bool = False,
) -> str:
    """在工作区内执行 shell 命令（构建、git、包管理等）。

    文件读写请优先使用 read_file / write_file 等结构化工具。
    background=true 时立即返回作业 id，随后用 process 查询或终止。
    """
    cfg = load_shell_config()
    if not cfg.enabled:
        return "shell 工具已在配置中禁用。"

    command = (command or "").strip()
    if not command:
        return "命令不能为空。"

    # 执行入口再次校验硬拒绝（与 policy gate 双保险；denied 时绝不建进程）
    from app.tools.packages.shell.guards import check_command_guard

    blocked = check_command_guard(command, safety_mode=cfg.safety_mode)
    if blocked:
        return blocked

    cwd, err = resolve_workdir(workdir, cfg.workdir)
    if err:
        return f"工作目录无效: {err}"
    assert cwd is not None

    env = build_subprocess_env(cfg.extra_env)
    session_id = tool_session_id.get()
    if not session_id:
        return "缺少 session，拒绝启动 shell 进程。"

    if background:
        job, error = execute_shell_background(command, cwd=cwd, env=env, session_id=session_id)
        if error or job is None:
            return error or "无法启动后台作业。"
        return f"已在后台启动作业 {job.id}（pid={job.pid}）。用 process(action=\"list\") 查看，process(action=\"kill\", job_id=\"{job.id}\") 终止。"

    effective_timeout = timeout if timeout is not None else cfg.default_timeout
    effective_timeout = max(1, min(int(effective_timeout), cfg.max_timeout))

    output, exit_code, error = run_with_progress(
        command,
        cwd=cwd,
        timeout=effective_timeout,
        env=env,
    )

    return format_shell_result(
        command,
        output,
        exit_code,
        session_id=session_id,
        error=error,
    )


def process(action: str, job_id: str = "", timeout: int = 30) -> str:
    """列出、等待或终止当前会话的后台 shell 作业。"""
    session_id = tool_session_id.get()
    action = (action or "").strip().lower()
    if action not in {"list", "wait", "kill"}:
        return "action 必须是 list、wait 或 kill。"

    if action == "list":
        jobs = list_jobs(session_id)
        if not jobs:
            return "当前会话没有登记的 shell 作业。"
        lines = ["job_id\tstatus\tcommand"]
        lines.extend(job.snapshot() for job in jobs)
        return "\n".join(lines)

    jid = (job_id or "").strip()
    if not jid:
        return "wait/kill 需要 job_id。"
    job = get_job(jid, session_id)
    if job is None:
        return f"作业不存在或不属于当前会话: {jid}"

    if action == "kill":
        kill_job(job)
        return f"已终止作业 {job.id}。"

    wait_timeout = max(1, min(int(timeout or 30), 300))
    proc = job.proc
    if proc is None or job.status != "running":
        body = job.output_text().strip()
        status = f"作业 {job.id} 状态={job.status}"
        if job.exit_code is not None:
            status += f" exit={job.exit_code}"
        return f"{status}\n\n{body}" if body else status

    try:
        proc.wait(timeout=wait_timeout)
    except Exception:
        return f"作业 {job.id} 仍在运行（等待 {wait_timeout}s 超时）。用 process(action=\"list\") 再查。"

    body = job.output_text().strip()
    code = proc.returncode or 0
    trailer = f"作业 {job.id} 已结束，退出码 {code}"
    return f"{body}\n\n{trailer}" if body else trailer
