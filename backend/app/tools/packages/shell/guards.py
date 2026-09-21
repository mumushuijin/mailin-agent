from __future__ import annotations

import re

from app.tools.packages.shell.envelope import CommandEnvelope, parse_command_envelope


def check_command_guard(command: str, *, safety_mode: bool = True) -> str | None:
    """返回硬拒绝原因；None 表示可进入审批/执行层。

    解析失败不在此硬拒绝（由上层转为 needs_approval）。
    """
    command = (command or "").strip()
    if not command:
        return "命令不能为空。"

    envelope = parse_command_envelope(command)

    if _credential_or_sidecar(command, envelope):
        return (
            "禁止通过 shell 直接访问凭证、敏感配置或 snapshot sidecar 路径。"
            "请使用 read_file / write_file / undo_file_change 或配置 API。"
        )

    if not safety_mode:
        return None

    hardline = _hardline_reason(command, envelope)
    if hardline:
        return f"安全策略拒绝执行：{hardline}"

    return None


def guard_decision(command: str, *, safety_mode: bool = True) -> dict:
    """可解释判定：deny / needs_approval / allow_candidate。"""
    envelope = parse_command_envelope(command)
    hard = check_command_guard(command, safety_mode=safety_mode)
    if hard:
        return {
            "outcome": "deny",
            "reason": hard,
            "envelope": envelope.to_public_dict(),
            "preview": _preview(envelope, hard),
        }
    if not envelope.parse_ok:
        reason = "命令无法可靠解析，需要显式审批"
        return {
            "outcome": "needs_approval",
            "reason": reason,
            "envelope": envelope.to_public_dict(),
            "preview": _preview(envelope, reason),
        }
    return {
        "outcome": "allow_candidate",
        "reason": "通过硬危险检查",
        "envelope": envelope.to_public_dict(),
        "preview": _preview(envelope, "待白名单/审批"),
    }


def _preview(envelope: CommandEnvelope, risk_reason: str) -> dict:
    return {
        "normalized_command": envelope.normalized_command(),
        "cwd": envelope.cwd,
        "capabilities": list(envelope.capabilities),
        "explicit_paths": list(envelope.explicit_paths),
        "parse_ok": envelope.parse_ok,
        "risk_reason": risk_reason,
        "timeout_seconds": envelope.timeout_seconds,
        "background": envelope.background,
    }


def _credential_or_sidecar(command: str, envelope: CommandEnvelope) -> bool:
    lowered = command.lower()
    patterns = (
        r"\.env\b",
        r"bootstraps[/\\]",
        r"sessions[/\\]",
        r"tool_results[/\\].*\.json",
        r"\.mailin[/\\]+snapshots\b",
        r"openai_api_key|dashscope_api_key|tavily_api_key",
    )
    if any(re.search(p, lowered) for p in patterns):
        return True
    if "sidecar_access" in envelope.capabilities:
        return True
    return False


def _hardline_reason(command: str, envelope: CommandEnvelope) -> str | None:
    lowered = command.lower()
    tokens = lowered.split()

    for i, tok in enumerate(tokens):
        if tok != "rm":
            continue
        has_rf = False
        for j in range(i + 1, len(tokens)):
            t = tokens[j]
            if t.startswith("-") and "r" in t and "f" in t:
                has_rf = True
            elif t in ("/", "/*"):
                if has_rf:
                    return "rm -rf / 类命令会破坏整个文件系统"
                break
            elif not t.startswith("-"):
                break

    if "if=/dev/zero" in lowered and "dd" in lowered:
        return "dd 写裸设备可能破坏磁盘数据"

    if re.search(r"\b(shutdown|reboot|halt|poweroff)\b", lowered):
        return "系统关机/重启命令被禁止"

    if "privilege_escalation" in envelope.capabilities:
        return "禁止提权命令（sudo/su）"

    return None
