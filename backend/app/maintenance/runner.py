from __future__ import annotations

import asyncio
import time
import uuid

from app.core.logging import get_current_trace_id, get_logger, log_scope, new_trace_id
from app.maintenance.tasks.mcp_health import check_mcp_health
from app.maintenance.tasks.memory_nudge import run_memory_nudge
from app.maintenance.tasks.memory_sediment import run_memory_sediment, sediment_would_run
from app.maintenance.types import MaintenanceResult
from app.services.push_service import push_maintenance

log = get_logger(__name__)

SEDIMENT_TIMEOUT_SECONDS = 180.0
SEDIMENT_SCHEDULE_COOLDOWN_SECONDS = 60.0


class MaintenanceRunner:
    """维护任务执行器：与聊天 run 解耦，统一推送 background 事件。"""

    def __init__(self) -> None:
        self._sediment_lock = asyncio.Lock()
        self._sediment_task: asyncio.Task | None = None
        self._last_sediment_attempt_at: float = 0.0

    async def notify(self, result: MaintenanceResult, *, force: bool = False) -> None:
        if not force and not result.should_notify:
            return
        await push_maintenance(
            result.kind,
            result.message,
            status=result.status,
            success=result.success,
            session_id=result.session_id,
            task_id=result.task_id,
            detail=result.detail or None,
        )

    async def run_sediment(self, *, notify: bool = True) -> MaintenanceResult:
        task_id = str(uuid.uuid4())
        trace_id = get_current_trace_id() or new_trace_id()
        start = time.monotonic()
        notified_start = False

        with log_scope(trace_id=trace_id, task_id=task_id, kind="sediment"):
            log.info("task.started")
            status = "done"
            success = True
            try:
                async with self._sediment_lock:
                    would_run = await asyncio.to_thread(sediment_would_run)
                    if not would_run:
                        result = MaintenanceResult(
                            kind="sediment",
                            message="暂无待沉淀内容",
                            status="skipped",
                            success=True,
                            task_id=task_id,
                        )
                        status = "skipped"
                        success = True
                        return result

                    if notify:
                        notified_start = True
                        await self.notify(
                            MaintenanceResult(
                                kind="sediment",
                                message="正在整理记忆沉淀…",
                                status="started",
                                success=True,
                                task_id=task_id,
                            )
                        )

                    try:
                        result = await asyncio.wait_for(
                            asyncio.to_thread(run_memory_sediment),
                            timeout=SEDIMENT_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        log.warning(
                            "task.timeout",
                            timeout_seconds=int(SEDIMENT_TIMEOUT_SECONDS),
                        )
                        status = "failed"
                        success = False
                        result = MaintenanceResult(
                            kind="sediment",
                            message=f"记忆沉淀超时（>{int(SEDIMENT_TIMEOUT_SECONDS)}s）",
                            status="failed",
                            success=False,
                        )
                    except Exception as exc:
                        log.error("task.failed", error=str(exc), exc_info=True)
                        status = "failed"
                        success = False
                        result = MaintenanceResult(
                            kind="sediment",
                            message="记忆沉淀失败",
                            status="failed",
                            success=False,
                            detail={"error": str(exc)},
                        )

                result.task_id = task_id
                status = result.status
                success = result.success
                if notify and (notified_start or result.should_notify):
                    await self.notify(
                        result,
                        force=notified_start and not result.should_notify,
                    )
                return result
            except Exception:
                status = "failed"
                success = False
                if notify and notified_start:
                    await self.notify(
                        MaintenanceResult(
                            kind="sediment",
                            message="记忆沉淀失败",
                            status="failed",
                            success=False,
                            task_id=task_id,
                        )
                    )
                raise
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                log.info(
                    "task.completed",
                    status=status,
                    success=success,
                    duration_ms=round(duration_ms, 1),
                )

    def schedule_sediment(self) -> None:
        """非阻塞触发沉淀（会话边界等场景）；已在执行时合并为一次。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        now = time.monotonic()
        if now - self._last_sediment_attempt_at < SEDIMENT_SCHEDULE_COOLDOWN_SECONDS:
            return
        if self._sediment_task and not self._sediment_task.done():
            return
        self._last_sediment_attempt_at = now
        self._sediment_task = loop.create_task(self._schedule_sediment_task())

    async def _schedule_sediment_task(self) -> None:
        try:
            await self.run_sediment(notify=True)
        except Exception:
            log.error("task.failed", kind="sediment", exc_info=True)

    async def run_memory_nudge_for_session(self, session_id: str) -> MaintenanceResult:
        task_id = str(uuid.uuid4())
        trace_id = get_current_trace_id() or new_trace_id()
        start = time.monotonic()
        status = "done"
        success = True

        with log_scope(trace_id=trace_id, task_id=task_id, kind="memory_nudge", session_id=session_id):
            log.info("task.started")
            await self.notify(
                MaintenanceResult(
                    kind="memory_nudge",
                    message="正在整理会话记忆…",
                    status="started",
                    success=True,
                    session_id=session_id,
                    task_id=task_id,
                )
            )
            try:
                result = await run_memory_nudge(session_id)
            except Exception as exc:
                log.error("task.failed", error=str(exc), exc_info=True)
                status = "failed"
                success = False
                result = MaintenanceResult(
                    kind="memory_nudge",
                    message="会话记忆整理失败",
                    status="failed",
                    success=False,
                    session_id=session_id,
                    detail={"error": str(exc)},
                )
            result.task_id = task_id
            status = result.status
            success = result.success
            await self.notify(result)
            duration_ms = (time.monotonic() - start) * 1000
            log.info(
                "task.completed",
                status=status,
                success=success,
                duration_ms=round(duration_ms, 1),
            )
            return result

    async def run_mcp_health(self) -> MaintenanceResult | None:
        task_id = str(uuid.uuid4())
        trace_id = get_current_trace_id() or new_trace_id()
        start = time.monotonic()
        status = "done"
        success = True

        with log_scope(trace_id=trace_id, task_id=task_id, kind="mcp_health"):
            log.info("task.started")
            try:
                result = await asyncio.to_thread(check_mcp_health)
                if result and result.should_notify:
                    result.task_id = task_id
                    await self.notify(result)
                    status = result.status
                    success = result.success
                elif result:
                    status = result.status
                    success = result.success
                else:
                    status = "skipped"
                return result
            except Exception as exc:
                status = "failed"
                success = False
                log.error("task.failed", error=str(exc), exc_info=True)
                raise
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                log.info(
                    "task.completed",
                    status=status,
                    success=success,
                    duration_ms=round(duration_ms, 1),
                )


_runner: MaintenanceRunner | None = None


def get_maintenance_runner() -> MaintenanceRunner:
    global _runner
    if _runner is None:
        _runner = MaintenanceRunner()
    return _runner
