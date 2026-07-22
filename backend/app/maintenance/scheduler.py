from __future__ import annotations

import asyncio
import logging

from app.maintenance.runner import get_maintenance_runner

logger = logging.getLogger(__name__)

SEDIMENT_INTERVAL_SECONDS = 300
MCP_HEALTH_INTERVAL_SECONDS = 600


class MaintenanceScheduler:
    """后台维护调度：定时执行沉淀与 MCP 健康检查。"""

    def __init__(
        self,
        *,
        sediment_interval: float = SEDIMENT_INTERVAL_SECONDS,
        mcp_health_interval: float = MCP_HEALTH_INTERVAL_SECONDS,
    ) -> None:
        self.sediment_interval = sediment_interval
        self.mcp_health_interval = mcp_health_interval
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="maintenance-scheduler")
        logger.info(
            "维护调度已启动（沉淀 %ds / MCP %ds）",
            int(self.sediment_interval),
            int(self.mcp_health_interval),
        )

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("维护调度已停止")

    async def _loop(self) -> None:
        runner = get_maintenance_runner()
        sediment_elapsed = 0.0
        mcp_elapsed = 0.0
        tick = 30.0

        try:
            while self._running:
                await asyncio.sleep(tick)
                if not self._running:
                    break

                sediment_elapsed += tick
                mcp_elapsed += tick

                if sediment_elapsed >= self.sediment_interval:
                    sediment_elapsed = 0.0
                    try:
                        await runner.run_sediment(notify=True)
                    except Exception:
                        logger.exception("定时沉淀失败")

                if mcp_elapsed >= self.mcp_health_interval:
                    mcp_elapsed = 0.0
                    try:
                        await runner.run_mcp_health()
                    except Exception:
                        logger.exception("MCP 健康检查失败")
        except asyncio.CancelledError:
            raise


_scheduler: MaintenanceScheduler | None = None


def get_maintenance_scheduler() -> MaintenanceScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = MaintenanceScheduler()
    return _scheduler
