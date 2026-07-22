"""内置钩子与注册入口。

register_hooks() 在应用启动（main.lifespan）时调用一次，把内置钩子注册到
全局 HookManager。是否启用由 workspace/CONFIG.json 的 hooks.enabled 控制
（缺省启用）。

内置钩子均为观察型（只记日志、不改变行为），用于验证钩子链路是否贯通，
同时作为编写自定义钩子的范例。
"""

from __future__ import annotations

import json
import logging

from app.agent.hooks.events import (
    POST_TOOL_CALL,
    PRE_TOOL_CALL,
)
from app.agent.hooks.manager import get_hook_manager, register_hook

logger = logging.getLogger(__name__)

_registered = False


def hooks_enabled() -> bool:
    """读取 CONFIG.json 的 hooks.enabled（缺省 True）。配置异常时保守启用。"""
    try:
        from app.tools.registry import load_full_config

        cfg = load_full_config()
    except Exception:
        return True
    hooks_cfg = cfg.get("hooks")
    if hooks_cfg is None:
        return True
    if isinstance(hooks_cfg, bool):
        return hooks_cfg
    if isinstance(hooks_cfg, dict):
        return hooks_cfg.get("enabled", True) not in (False, "false", "0", "off")
    return True


# ---------------------------------------------------------------------------
# 内置观察钩子
# ---------------------------------------------------------------------------


def _guard_run_shell(tool_name: str = "", args: dict | None = None, **kwargs) -> dict | None:
    if tool_name != "run_shell":
        return None
    from app.tools.packages.shell.config import load_shell_config
    from app.tools.packages.shell.guards import check_command_guard

    command = (args or {}).get("command", "")
    cfg = load_shell_config()
    reason = check_command_guard(str(command), safety_mode=cfg.safety_mode)
    if reason:
        return {"action": "block", "message": reason}
    return None


def _audit_tool_call(tool_name: str = "", args: dict | None = None, session_id: str | None = None, **kwargs) -> None:
    """审计每次工具调用（截断参数，避免日志膨胀）。"""
    try:
        args_text = json.dumps(args or {}, ensure_ascii=False)[:200]
    except Exception:
        args_text = str(args)[:200]
    logger.info("[hook] pre_tool_call session=%s tool=%s args=%s", session_id, tool_name, args_text)


def _log_tool_result(
    tool_name: str = "",
    result: str = "",
    session_id: str | None = None,
    duration_ms: int | None = None,
    **kwargs,
) -> None:
    """记录工具执行结果长度与耗时。"""
    logger.info(
        "[hook] post_tool_call session=%s tool=%s chars=%d duration_ms=%s",
        session_id,
        tool_name,
        len(result or ""),
        duration_ms,
    )


def register_hooks(*, force: bool = False) -> bool:
    """注册内置钩子。返回是否已注册（启用）。幂等。"""
    global _registered
    if _registered and not force:
        return True

    if not hooks_enabled():
        logger.info("钩子系统已禁用（CONFIG.json hooks.enabled=false），跳过注册")
        _registered = True
        return False

    if force:
        get_hook_manager().clear()

    register_hook(PRE_TOOL_CALL, _guard_run_shell)
    register_hook(PRE_TOOL_CALL, _audit_tool_call)
    register_hook(POST_TOOL_CALL, _log_tool_result)

    _registered = True
    logger.info("内置钩子已注册")
    return True
