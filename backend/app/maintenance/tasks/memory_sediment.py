from __future__ import annotations

from datetime import datetime

from app.context.memory.consolidator import _memory_config
from app.context.memory.maintenance import maybe_auto_consolidate
from app.context.memory.sediment_state import SedimentState
from app.core.settings import get_settings
from app.maintenance.types import MaintenanceResult
from app.storage.workspace import MemoryStore


def sediment_would_run(workspace=None) -> bool:
    """快速预检：是否有待沉淀内容且已过最小间隔（无 LLM 调用）。"""
    workspace = workspace or get_settings().workspace_path
    cfg = _memory_config(workspace)
    days = int(cfg.get("longterm_consolidate_days", 7))
    store = MemoryStore(workspace)
    sediment_state = SedimentState(workspace)
    if not sediment_state.pending_entries(store, days=days):
        return False
    interval_h = int(
        cfg.get("consolidate_interval_hours", cfg.get("longterm_consolidate_interval_hours", 24))
    )
    last = sediment_state.last_consolidate_at()
    if last and (datetime.now() - last).total_seconds() < interval_h * 3600:
        return False
    return True


def run_memory_sediment() -> MaintenanceResult:
    workspace = get_settings().workspace_path
    raw = maybe_auto_consolidate(workspace)

    if raw is None:
        return MaintenanceResult(
            kind="sediment",
            message="暂无待沉淀内容",
            status="skipped",
            success=True,
        )

    if "跳过" in raw:
        return MaintenanceResult(
            kind="sediment",
            message=raw,
            status="skipped",
            success=True,
            detail={"reason": "interval"},
        )

    return MaintenanceResult(
        kind="sediment",
        message=raw if len(raw) <= 120 else f"{raw[:117]}…",
        status="done",
        success=True,
        detail={"summary": raw},
    )
