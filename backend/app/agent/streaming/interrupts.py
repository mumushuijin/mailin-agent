from __future__ import annotations

from typing import Any


async def get_pending_interrupts(graph, config: dict) -> list[Any]:
    """读取 checkpoint 中尚未恢复的 interrupt 值。"""
    snapshot = await graph.aget_state(config)
    if not snapshot or not snapshot.tasks:
        return []
    values: list[Any] = []
    for task in snapshot.tasks:
        for intr in task.interrupts:
            values.append(intr.value)
    return values


async def has_pending_interrupt(graph, config: dict) -> bool:
    return bool(await get_pending_interrupts(graph, config))
