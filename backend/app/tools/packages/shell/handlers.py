from __future__ import annotations

from app.tools.packages.shell.config import load_shell_config
from app.tools.packages.shell.executor import format_shell_result, resolve_workdir, run_with_progress
from app.tools.packages.shell.env import build_subprocess_env


def run_shell(command: str, timeout: int | None = None, workdir: str | None = None) -> str:
    """在工作区内执行 shell 命令（构建、git、包管理等）。

    文件读写请优先使用 read_file / write_file 等结构化工具。
    """
    cfg = load_shell_config()
    if not cfg.enabled:
        return "shell 工具已在配置中禁用。"

    command = (command or "").strip()
    if not command:
        return "命令不能为空。"

    cwd, err = resolve_workdir(workdir, cfg.workdir)
    if err:
        return f"工作目录无效: {err}"
    assert cwd is not None

    effective_timeout = timeout if timeout is not None else cfg.default_timeout
    effective_timeout = max(1, min(int(effective_timeout), cfg.max_timeout))

    env = build_subprocess_env(cfg.extra_env)
    output, exit_code, error = run_with_progress(
        command,
        cwd=cwd,
        timeout=effective_timeout,
        env=env,
    )

    from app.tools.runtime import tool_session_id

    return format_shell_result(
        command,
        output,
        exit_code,
        session_id=tool_session_id.get(),
        error=error,
    )
