from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from app.agent.approval_context import approval_enabled
from app.agent.graph import get_graph, make_thread_config
from app.agent.streaming.events import AgentEvent
from app.agent.streaming.interrupts import get_pending_interrupts, has_pending_interrupt
from app.core.logging import get_logger, log_scope, new_trace_id
from app.services.chat_service import ChatService
from app.services.ws_manager import ConnectionManager

log = get_logger(__name__)


class WsChatService:
    def __init__(self, manager: ConnectionManager, chat_service: ChatService | None = None):
        self.manager = manager
        self.chat_service = chat_service or ChatService()
        self._active_runs: dict[str, asyncio.Task] = {}
        self._approval_futures: dict[str, asyncio.Future] = {}
        self._run_sessions: dict[str, str] = {}

    async def handle_message(self, conn_id: int, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        if op == "ping":
            await self.manager.send_event(conn_id, AgentEvent("pong", {}))
            return

        if op == "session.subscribe":
            session_id = payload.get("session_id")
            self.manager.subscribe_session(conn_id, session_id)
            return

        if op == "chat.send":
            run_id = payload.get("run_id") or str(uuid.uuid4())
            message = payload.get("message", "")
            session_id = payload.get("session_id")
            await self._start_run(conn_id, run_id, message, session_id)
            return

        if op == "chat.cancel":
            run_id = payload.get("run_id")
            if run_id:
                await self.cancel_run(run_id)
            return

        if op == "approve":
            run_id = payload.get("run_id")
            if run_id and run_id in self._approval_futures:
                future = self._approval_futures.pop(run_id)
                if not future.done():
                    if payload.get("kind") == "ask_user":
                        future.set_result({"kind": "ask_user", "answer": payload.get("answer")})
                    else:
                        decision = payload.get("decision", "deny")
                        future.set_result("allow" if decision == "allow" else "deny")
            return

        await self.manager.send_event(
            conn_id,
            AgentEvent("error", {"error": f"未知操作: {op}"}),
        )

    async def _start_run(
        self,
        conn_id: int,
        run_id: str,
        message: str,
        session_id: str | None,
    ) -> None:
        existing = self._active_runs.get(run_id)
        if existing and not existing.done():
            await self.manager.send_event(
                conn_id,
                AgentEvent("error", {"error": "该 run 仍在执行中"}, run_id),
            )
            return

        task = asyncio.create_task(self._execute_run(conn_id, run_id, message, session_id))
        self._active_runs[run_id] = task

    async def _execute_run(
        self,
        conn_id: int,
        run_id: str,
        message: str,
        session_id: str | None,
    ) -> None:
        start = time.monotonic()
        status = "success"
        sid: str | None = session_id
        token = approval_enabled.set(True)
        cancel_notified = False

        with log_scope(
            trace_id=new_trace_id(),
            run_id=run_id,
            session_id=session_id,
            conn_id=conn_id,
        ):
            log.info("run.started")
            try:
                async for event in self.chat_service.iter_chat_events(
                    message, session_id, run_id=run_id
                ):
                    if event.type == "session" and event.data.get("session_id"):
                        sid = event.data["session_id"]
                        self._run_sessions[run_id] = sid
                        self.manager.subscribe_session(conn_id, sid)
                    if event.data.get("cancelled") and event.type in ("error", "done"):
                        cancel_notified = True
                    await self.manager.send_event(conn_id, event)

                if sid:
                    await self._handle_interrupts(conn_id, run_id, sid)

            except asyncio.CancelledError:
                status = "cancelled"
                if sid:
                    await self.chat_service.repair_cancelled_checkpoint(sid)
                if not cancel_notified:
                    await self._emit_cancelled(conn_id, run_id, sid)
            except Exception as e:
                status = "failed"
                log.error("run.failed", error=str(e), exc_info=True)
                await self.manager.send_event(
                    conn_id,
                    AgentEvent("error", {"error": str(e)}, run_id),
                )
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                log.info(
                    "run.completed",
                    status=status,
                    duration_ms=round(duration_ms, 1),
                    cancelled=(status == "cancelled"),
                    session_id=sid,
                )
                approval_enabled.reset(token)
                self._active_runs.pop(run_id, None)
                self._approval_futures.pop(run_id, None)
                self._run_sessions.pop(run_id, None)

    async def _handle_interrupts(self, conn_id: int, run_id: str, session_id: str) -> None:
        graph = await asyncio.to_thread(get_graph)
        config = make_thread_config(session_id)

        while await has_pending_interrupt(graph, config):
            interrupts = await get_pending_interrupts(graph, config)
            if not interrupts:
                break

            intr = interrupts[0]
            if isinstance(intr, dict):
                interrupt_data = intr
            else:
                interrupt_data = {"value": intr}

            await self.manager.send_event(
                conn_id,
                AgentEvent("interrupt", interrupt_data, run_id),
            )

            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            self._approval_futures[run_id] = future

            try:
                decision = await asyncio.wait_for(future, timeout=300.0)
            except TimeoutError:
                decision = "deny"
                await self.manager.send_event(
                    conn_id,
                    AgentEvent("error", {"error": "审批超时，已自动拒绝"}, run_id),
                )

            async for event in self.chat_service.resume_chat_events(
                session_id, decision, run_id=run_id
            ):
                await self.manager.send_event(conn_id, event)

            graph = await asyncio.to_thread(get_graph)

    async def cancel_run(self, run_id: str) -> None:
        future = self._approval_futures.pop(run_id, None)
        if future and not future.done():
            future.cancel()

        task = self._active_runs.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        session_id = self._run_sessions.get(run_id)
        if session_id:
            from app.tools.packages.shell.process_registry import kill_session_processes

            kill_session_processes(session_id)
            await self.chat_service.repair_cancelled_checkpoint(session_id)

    async def _emit_cancelled(
        self,
        conn_id: int,
        run_id: str,
        session_id: str | None,
    ) -> None:
        """取消时推送成对终态事件，便于前端清理工具/loading 状态。"""
        await self.manager.send_event(
            conn_id,
            AgentEvent("error", {"error": "已取消", "cancelled": True}, run_id),
        )
        await self.manager.send_event(
            conn_id,
            AgentEvent(
                "done",
                {
                    "content": "",
                    "session_id": session_id,
                    "partial": True,
                    "cancelled": True,
                },
                run_id,
            ),
        )
