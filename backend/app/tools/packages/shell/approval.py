from __future__ import annotations

from typing import Any

from app.tools.packages.shell.allowlist import evaluate_shell_auto_approve
from app.tools.runtime import tool_session_id


def should_auto_approve_run_shell(
    command: str,
    *,
    cwd: str | None = None,
    background: bool = False,
    timeout_seconds: float | None = None,
    session_id: str | None = None,
) -> bool:
    sid = session_id if session_id is not None else tool_session_id.get()
    matched, _env, _reason, _preview = evaluate_shell_auto_approve(
        command,
        session_id=sid,
        cwd=cwd,
        background=background,
        timeout_seconds=timeout_seconds,
    )
    return matched


def build_shell_approval_preview(
    command: str,
    *,
    cwd: str | None = None,
    background: bool = False,
    timeout_seconds: float | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    sid = session_id if session_id is not None else tool_session_id.get()
    matched, envelope, reason, preview = evaluate_shell_auto_approve(
        command,
        session_id=sid,
        cwd=cwd,
        background=background,
        timeout_seconds=timeout_seconds,
    )
    preview.update(
        {
            "allowlist_matched": matched,
            "risk_reason": reason if not matched else "白名单已匹配",
            "operation": "run_shell",
            "target_summary": envelope.normalized_command()[:300],
            "timeout_seconds": timeout_seconds or envelope.timeout_seconds,
            "cwd": cwd or envelope.cwd or ".",
        }
    )
    return preview
