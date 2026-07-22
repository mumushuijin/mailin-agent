from __future__ import annotations

from app.tools.packages.shell.config import load_shell_config, matches_auto_approve


def should_auto_approve_run_shell(command: str) -> bool:
    cfg = load_shell_config()
    return matches_auto_approve(command, cfg.auto_approve_patterns)
