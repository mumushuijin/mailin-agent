from __future__ import annotations

from typing import Any


async def get_pending_interrupts(graph, config: dict) -> list[Any]:
    """读取 checkpoint 中尚未恢复的 interrupt 值。"""
    snapshot = await graph.aget_state(config)
    if not snapshot:
        return []
    if not getattr(snapshot, "tasks", None):
        # MemorySaver and test graphs may expose interrupts only in a task snapshot
        # after the stream iterator has yielded; inspect values as a conservative fallback.
        return list((getattr(snapshot, "values", {}) or {}).get("pending_interrupts") or [])
    values: list[Any] = []
    for task in getattr(snapshot, "tasks", []) or []:
        for intr in getattr(task, "interrupts", []) or []:
            values.append(intr.value)
    return values


async def has_pending_interrupt(graph, config: dict) -> bool:
    return bool(await get_pending_interrupts(graph, config))
