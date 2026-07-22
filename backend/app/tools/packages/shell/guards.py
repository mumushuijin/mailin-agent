from __future__ import annotations

import re


def check_command_guard(command: str, *, safety_mode: bool = True) -> str | None:
    """返回拦截原因；None 表示放行到审批/执行层。"""
    command = (command or "").strip()
    if not command:
        return "命令不能为空。"

    if _credential_access(command):
        return (
            "禁止通过 shell 直接访问凭证或敏感配置路径。"
            "请使用 read_file / write_file 或配置 API。"
        )

    if not safety_mode:
        return None

    hardline = _hardline_reason(command)
    if hardline:
        return f"安全策略拒绝执行：{hardline}"

    return None


def _credential_access(command: str) -> bool:
    lowered = command.lower()
    patterns = (
        r"\.env\b",
        r"bootstraps[/\\]",
        r"sessions[/\\]",
        r"tool_results[/\\].*\.json",
        r"openai_api_key|dashscope_api_key|tavily_api_key",
    )
    return any(re.search(p, lowered) for p in patterns)


def _hardline_reason(command: str) -> str | None:
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

    return None
