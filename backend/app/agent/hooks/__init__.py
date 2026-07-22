"""进程内钩子系统。

在 LangGraph ReAct 执行流的关键生命周期点触发自定义回调，支持：
- 观察型：日志 / 指标 / 通知（返回值忽略）
- 干预型：pre_tool_call 拦截工具、pre_llm_call 注入上下文、transform_tool_result 改写结果
"""

from __future__ import annotations

from app.agent.hooks.builtins import hooks_enabled, register_hooks
from app.agent.hooks.events import (
    ON_SESSION_END,
    ON_SESSION_START,
    POST_LLM_CALL,
    POST_TOOL_CALL,
    PRE_AGENT_STEP,
    PRE_LLM_CALL,
    PRE_TOOL_CALL,
    TRANSFORM_TOOL_RESULT,
    VALID_HOOKS,
)
from app.agent.hooks.manager import (
    HookManager,
    dispatch_observe,
    dispatch_post_llm_call,
    dispatch_post_tool_call,
    dispatch_pre_agent_step,
    dispatch_pre_llm_call,
    dispatch_pre_tool_call,
    dispatch_transform_tool_result,
    get_hook_manager,
    register_hook,
)

__all__ = [
    "HookManager",
    "get_hook_manager",
    "register_hook",
    "register_hooks",
    "hooks_enabled",
    "dispatch_observe",
    "dispatch_pre_tool_call",
    "dispatch_post_tool_call",
    "dispatch_pre_agent_step",
    "dispatch_pre_llm_call",
    "dispatch_post_llm_call",
    "dispatch_transform_tool_result",
    "ON_SESSION_START",
    "ON_SESSION_END",
    "PRE_LLM_CALL",
    "POST_LLM_CALL",
    "PRE_AGENT_STEP",
    "PRE_TOOL_CALL",
    "POST_TOOL_CALL",
    "TRANSFORM_TOOL_RESULT",
    "VALID_HOOKS",
]
