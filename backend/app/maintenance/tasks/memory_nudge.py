from __future__ import annotations

import asyncio
import logging

from app.agent.graph import get_graph, make_thread_config
from app.context.memory.maintenance import build_memory_nudge_message
from app.maintenance.types import MaintenanceResult
from app.resilience import GRAPH_REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


async def run_memory_nudge(session_id: str) -> MaintenanceResult:
    graph = await asyncio.to_thread(get_graph)
    config = make_thread_config(session_id)

    try:
        await asyncio.wait_for(
            graph.ainvoke(
                {"messages": [build_memory_nudge_message()], "memory_nudge_pending": False},
                config,
            ),
            timeout=GRAPH_REQUEST_TIMEOUT_SECONDS,
        )
        return MaintenanceResult(
            kind="memory_nudge",
            message="会话记忆整理完成",
            status="done",
            success=True,
            session_id=session_id,
        )
    except Exception as exc:
        logger.warning("memory nudge 失败: %s", exc)
        return MaintenanceResult(
            kind="memory_nudge",
            message="会话记忆整理失败",
            status="failed",
            success=False,
            session_id=session_id,
            detail={"error": str(exc)},
        )
