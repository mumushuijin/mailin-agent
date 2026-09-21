"""工具执行安全策略：配置迁移与 enforcement 诊断。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def resolve_enforcement_mode(config: dict | None) -> str:
    """新 workspace 默认 enforce；显式配置优先。"""
    if not isinstance(config, dict):
        return "enforce"
    tools = config.get("tools")
    if not isinstance(tools, dict):
        return "enforce"
    mode = tools.get("enforcement_mode")
    if mode in ("audit", "enforce"):
        return mode
    policy = tools.get("policy")
    if isinstance(policy, dict) and policy.get("enforcement_mode") in ("audit", "enforce"):
        return policy["enforcement_mode"]
    return "enforce"


def diagnose_legacy_tool_config(config: dict | None) -> list[str]:
    """旧 workspace 宽松配置诊断提示（不改变硬边界）。"""
    hints: list[str] = []
    if not isinstance(config, dict):
        return hints
    tools = config.get("tools")
    if not isinstance(tools, dict):
        return hints

    if "enforcement_mode" not in tools:
        hints.append(
            "未设置 tools.enforcement_mode；新默认值为 enforce。"
            "迁移期可临时设为 audit 以观察非硬边界拒绝，硬边界仍会拒绝。"
        )

    shell = tools.get("shell")
    if isinstance(shell, dict):
        patterns = shell.get("auto_approve_patterns") or []
        policy = shell.get("policy")
        if patterns and not (isinstance(policy, dict) and policy.get("commands")):
            hints.append(
                "检测到旧 auto_approve_patterns 且无结构化 shell.policy.commands；"
                "patterns 仅作兼容 fallback，不能放宽 workspace/凭证/sidecar/超时硬边界。"
            )
        if shell.get("safety_mode") in (False, "false", "0", "off"):
            hints.append("shell.safety_mode=false 会跳过部分硬线检查，强烈建议保持开启。")

    fs = tools.get("filesystem")
    if fs is True or fs is None:
        hints.append(
            "filesystem 仍为布尔开关；可升级为对象并启用 snapshots.retention 默认值。"
        )
    elif isinstance(fs, dict):
        snaps = fs.get("snapshots")
        if not isinstance(snaps, dict) or snaps.get("enabled") is None:
            hints.append("建议在 tools.filesystem.snapshots 中显式启用快照与 retention。")

    return hints


def apply_migration_hints(config: dict | None) -> dict[str, Any]:
    """记录诊断并返回摘要；硬边界永不因 audit 放宽。"""
    mode = resolve_enforcement_mode(config)
    hints = diagnose_legacy_tool_config(config)
    for hint in hints:
        logger.info("[policy-migration] %s", hint)
    return {
        "enforcement_mode": mode,
        "hints": hints,
        "hard_boundaries_always_on": True,
    }
