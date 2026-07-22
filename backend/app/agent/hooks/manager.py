"""进程内钩子管理器与分发器。

设计要点（对齐 hermes plugin hooks，裁剪为本项目所需）：
- 单例 HookManager，线程安全（LangGraph 同步节点可能在 worker 线程执行）。
- 回调一律以关键字参数调用，回调签名须带 **kwargs 保证前向兼容。
- 任一回调抛异常只记日志、绝不中断 agent 主循环。
- 干预型事件的聚合语义：
    * pre_tool_call：首个 block 生效（Python 内置钩子先注册、优先级更高）。
    * pre_llm_call：收集所有非空 context，按注册序用双换行拼接。
    * transform_tool_result：首个非空返回值替换结果。
  其余事件为观察型，返回值忽略。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from app.agent.hooks.events import (
    POST_LLM_CALL,
    POST_TOOL_CALL,
    PRE_AGENT_STEP,
    PRE_LLM_CALL,
    PRE_TOOL_CALL,
    TRANSFORM_TOOL_RESULT,
    VALID_HOOKS,
)

logger = logging.getLogger(__name__)

Callback = Callable[..., Any]


class HookManager:
    def __init__(self) -> None:
        self._hooks: dict[str, list[Callback]] = {}
        self._lock = threading.RLock()

    def register(self, event: str, callback: Callback) -> bool:
        if event not in VALID_HOOKS:
            logger.warning("忽略未知钩子事件: %s", event)
            return False
        with self._lock:
            self._hooks.setdefault(event, []).append(callback)
        logger.debug("已注册钩子: %s -> %s", event, getattr(callback, "__name__", callback))
        return True

    def callbacks(self, event: str) -> list[Callback]:
        with self._lock:
            return list(self._hooks.get(event, ()))

    def has(self, event: str) -> bool:
        with self._lock:
            return bool(self._hooks.get(event))

    def clear(self) -> None:
        with self._lock:
            self._hooks.clear()


_manager = HookManager()


def get_hook_manager() -> HookManager:
    return _manager


def register_hook(event: str, callback: Callback) -> bool:
    return _manager.register(event, callback)


def _safe_call(event: str, callback: Callback, kwargs: dict[str, Any]) -> Any:
    try:
        return callback(**kwargs)
    except Exception:
        logger.exception(
            "钩子回调异常 event=%s callback=%s",
            event,
            getattr(callback, "__name__", callback),
        )
        return None


# ---------------------------------------------------------------------------
# 观察型分发
# ---------------------------------------------------------------------------


def dispatch_observe(event: str, **kwargs: Any) -> None:
    for cb in _manager.callbacks(event):
        _safe_call(event, cb, kwargs)


# ---------------------------------------------------------------------------
# 干预型分发
# ---------------------------------------------------------------------------


def _normalize_block(result: Any) -> str | None:
    """归一化 pre_tool_call 的 block 返回。

    同时接受 Hermes 规范（{"action":"block","message":...}）与
    Claude-Code 风格（{"decision":"block","reason":...}）。
    返回非空拦截原因字符串，或 None 表示放行。
    """
    if not isinstance(result, dict):
        return None
    action = result.get("action")
    decision = result.get("decision")
    if action == "block" or decision == "block":
        message = result.get("message") or result.get("reason") or "钩子拦截了此工具调用。"
        text = str(message).strip()
        return text or "钩子拦截了此工具调用。"
    return None


def dispatch_pre_tool_call(**kwargs: Any) -> str | None:
    """返回首个 block 原因字符串；None 表示放行。"""
    for cb in _manager.callbacks(PRE_TOOL_CALL):
        result = _safe_call(PRE_TOOL_CALL, cb, kwargs)
        reason = _normalize_block(result)
        if reason:
            return reason
    return None


def dispatch_post_tool_call(**kwargs: Any) -> None:
    dispatch_observe(POST_TOOL_CALL, **kwargs)


def dispatch_pre_agent_step(**kwargs: Any) -> None:
    dispatch_observe(PRE_AGENT_STEP, **kwargs)


def dispatch_post_llm_call(**kwargs: Any) -> None:
    dispatch_observe(POST_LLM_CALL, **kwargs)


def _normalize_context(result: Any) -> str | None:
    if isinstance(result, dict):
        ctx = result.get("context")
        if isinstance(ctx, str) and ctx.strip():
            return ctx.strip()
        return None
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def dispatch_pre_llm_call(**kwargs: Any) -> str | None:
    """收集所有非空注入上下文，按注册序用双换行拼接；无则返回 None。"""
    contexts: list[str] = []
    for cb in _manager.callbacks(PRE_LLM_CALL):
        result = _safe_call(PRE_LLM_CALL, cb, kwargs)
        ctx = _normalize_context(result)
        if ctx:
            contexts.append(ctx)
    if not contexts:
        return None
    return "\n\n".join(contexts)


def dispatch_transform_tool_result(**kwargs: Any) -> str | None:
    """返回首个非空改写结果；None 表示保持原结果不变。"""
    for cb in _manager.callbacks(TRANSFORM_TOOL_RESULT):
        result = _safe_call(TRANSFORM_TOOL_RESULT, cb, kwargs)
        if isinstance(result, str) and result:
            return result
    return None
