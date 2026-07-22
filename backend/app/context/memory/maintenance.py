"""会话级记忆维护：自动沉淀、周期 nudge。"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.messages import MEMORY_NUDGE_TEMPLATE, make_system_message
from app.context.budget import load_context_config
from app.context.memory.consolidator import consolidate_to_longterm
from app.core.settings import get_settings

logger = logging.getLogger(__name__)


def maybe_auto_consolidate(workspace=None) -> str | None:
    """会话边界尝试 24h 热层沉淀（不足间隔则跳过）。"""
    workspace = workspace or get_settings().workspace_path
    try:
        result = consolidate_to_longterm(workspace)
        if result and "跳过" not in result and result != "无待沉淀内容":
            logger.info("自动沉淀: %s", result)
            return result
    except Exception as exc:
        logger.warning("自动沉淀失败: %s", exc)
    return None


def build_memory_nudge_message() -> Any:
    cfg = load_context_config().get("memory", {})
    warm = cfg.get("warm", {})
    turns = int(warm.get("nudge_every_user_turns", cfg.get("daily_sediment_every_turns", 5)))
    return make_system_message(MEMORY_NUDGE_TEMPLATE.format(turns=turns), "memory_nudge")


async def maybe_run_memory_nudge(graph, config: dict, result: dict) -> dict:
    """主对话结束后，若 pending 则追加系统维护轮次。"""
    if not result.get("memory_nudge_pending"):
        return result
    nudge = build_memory_nudge_message()
    try:
        nudge_result = await graph.ainvoke(
            {"messages": [nudge], "memory_nudge_pending": False},
            config,
        )
        return {**result, **{k: v for k, v in nudge_result.items() if k != "messages"}, "messages": nudge_result.get("messages", [])}
    except Exception as exc:
        logger.warning("memory nudge 失败: %s", exc)
        return result


async def prepare_session_memory(message: str) -> None:
    """会话开始前的非阻塞记忆维护（批量温层沉淀）。"""
    from app.maintenance.runner import get_maintenance_runner

    get_maintenance_runner().schedule_sediment()
