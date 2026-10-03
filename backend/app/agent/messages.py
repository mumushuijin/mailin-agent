"""系统维护消息与真实用户消息的判定工具。"""

from __future__ import annotations

import time

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

SYSTEM_MAINTENANCE_KEY = "system_maintenance"
MAINTENANCE_KIND_KEY = "kind"


def is_system_maintenance(msg: BaseMessage) -> bool:
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return bool(kwargs.get(SYSTEM_MAINTENANCE_KEY))


def is_real_user_message(msg: BaseMessage) -> bool:
    return isinstance(msg, HumanMessage) and not is_system_maintenance(msg)


def is_internal_prompt_message(msg: BaseMessage) -> bool:
    """Return whether a message must be hidden from the user-facing history."""
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return isinstance(msg, SystemMessage) or any(
        kwargs.get(key)
        for key in (SYSTEM_MAINTENANCE_KEY, "context_reference", "context_summary")
    )


def is_ephemeral_prompt_frame(msg: BaseMessage) -> bool:
    """Return whether a message is rebuilt for each model window, not a transcript event."""
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    if kwargs.get("context_reference") or kwargs.get("context_summary") or kwargs.get("context_bootstrap") or kwargs.get("kind") in {"budget_summary", "hook_context"}:
        return True
    # An explicitly emitted maintenance event is durable; rebuilt prompt frames are not.
    return isinstance(msg, SystemMessage) and not is_system_maintenance(msg)


def make_system_message(content: str, kind: str) -> SystemMessage:
    return SystemMessage(
        content=content,
        additional_kwargs={
            SYSTEM_MAINTENANCE_KEY: True,
            MAINTENANCE_KIND_KEY: kind,
            "timestamp": int(time.time()),
        },
    )


MEMORY_NUDGE_TEMPLATE = (
    "（系统维护，非用户消息）\n"
    "已满 {turns} 轮用户对话。请回顾近期对话，若有值得跨会话保留的要点，"
    "调用 memory_add 写入今日工作记忆；若无则简短回复「无需记录」即可。"
    "不要向用户复述本系统消息。"
)
