from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from app.agent.approval_context import approval_enabled
from app.agent.state import public_state_projection
from app.agent.graph import get_graph, make_thread_config
from app.agent.streaming.events import AgentEvent
from app.agent.streaming.dispatcher import DispatchEvent, RunEventDispatcher
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
        self._pending_interrupt_meta: dict[str, dict[str, Any]] = {}
        self._dispatchers: dict[str, RunEventDispatcher] = {}
        self._dispatch_conn: dict[str, int] = {}

    async def _send_run_event(self, run_id: str, event: AgentEvent) -> bool:
        dispatcher = self._dispatchers.get(run_id)
        if dispatcher is None:
            conn_id = self._dispatch_conn.get(run_id)
            if conn_id is not None:
                await self.manager.send_event(conn_id, event)
            return False
        return await dispatcher.publish(
            event.type,
            {"agent_event": event},
            state=event if event.state is not None else None,
        )

    async def _run_dispatcher(self, run_id: str, conn_id: int, dispatcher: RunEventDispatcher) -> None:
        async def send(item: DispatchEvent) -> None:
            value = item.payload.get("agent_event")
            if not isinstance(value, AgentEvent):
                return
            if item.kind == "state_sync":
                value = AgentEvent("state_sync", value.data, run_id, state=value.state)
            await self.manager.send_event(conn_id, value)

        await dispatcher.run(send)

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

        if op == "state.sync":
            run_id = payload.get("run_id")
            session_id = payload.get("session_id")
            if not run_id or not session_id or self._run_sessions.get(run_id) != session_id:
                return
            graph = await asyncio.to_thread(get_graph)
            snapshot = await graph.aget_state(make_thread_config(session_id))
            canonical = dict(snapshot.values or {}) if snapshot else {}
            if canonical and (canonical.get("scope") or {}).get("run_id") == run_id:
                await self._send_run_event(
                    run_id,
                    AgentEvent(
                        "state_sync", {"session_id": session_id}, run_id,
                        state=public_state_projection(canonical),
                    ),
                )
            return

        if op == "approve":
            run_id = payload.get("run_id")
            if not run_id or run_id not in self._approval_futures:
                if run_id:
                    await self.manager.send_event(
                        conn_id,
                        AgentEvent(
                            "error",
                            {
                                "error": "审批或问答响应已过期",
                                "fail_closed": True,
                                "reason_code": "stale_interrupt",
                            },
                            run_id,
                        ),
                    )
                return
            expected_session = self._run_sessions.get(run_id)
            payload_session = payload.get("session_id")
            if expected_session and payload_session and payload_session != expected_session:
                log.warning(
                    "stale approval ignored: session mismatch run=%s",
                    run_id,
                )
                await self.manager.send_event(
                    conn_id,
                    AgentEvent(
                        "error",
                        {
                            "error": "响应与当前 session 不匹配",
                            "fail_closed": True,
                            "reason_code": "stale_session",
                        },
                        run_id,
                    ),
                )
                return

            pending = self._pending_interrupt_meta.get(run_id) or {}
            expected_step = pending.get("step_id")
            provided_step = payload.get("step_id")
            if expected_step and provided_step != expected_step:
                await self.manager.send_event(
                    conn_id,
                    AgentEvent(
                        "error",
                        {"error": "响应与当前 step 不匹配", "fail_closed": True, "reason_code": "stale_step"},
                        run_id,
                    ),
                )
                return
            future = self._approval_futures.get(run_id)
            if future is None:
                return
            if future.done():
                return

            if payload.get("kind") == "ask_user":
                interrupt_id = payload.get("interrupt_id")
                expected_iid = pending.get("interrupt_id")
                if expected_iid and interrupt_id != expected_iid:
                    log.warning("stale ask_user ignored: interrupt_id mismatch run=%s", run_id)
                    await self.manager.send_event(
                        conn_id,
                        AgentEvent(
                            "error",
                            {
                                "error": "问答响应与当前中断不匹配",
                                "fail_closed": True,
                                "reason_code": "stale_interrupt",
                            },
                            run_id,
                        ),
                    )
                    return
                tool_call_id = payload.get("tool_call_id")
                expected_tc = pending.get("tool_call_id")
                if expected_tc and tool_call_id != expected_tc:
                    log.warning("stale ask_user ignored: tool_call_id mismatch run=%s", run_id)
                    await self.manager.send_event(
                        conn_id,
                        AgentEvent(
                            "error",
                            {
                                "error": "问答响应与当前工具调用不匹配",
                                "fail_closed": True,
                                "reason_code": "stale_tool_call",
                            },
                            run_id,
                        ),
                    )
                    return
                mode = payload.get("mode") or pending.get("mode") or "answer_and_continue"
                result: dict[str, Any] = {
                    "kind": "ask_user",
                    "mode": mode,
                    "interrupt_id": interrupt_id or expected_iid,
                    "tool_call_id": tool_call_id or expected_tc,
                }
                if mode == "handoff_and_stop":
                    result["ack"] = True
                else:
                    result["answer"] = payload.get("answer")
                future.set_result(result)
            else:
                # 工具审批：校验 tool_call_id
                tool_call_id = payload.get("tool_call_id")
                expected_tc = pending.get("tool_call_id")
                if expected_tc and tool_call_id != expected_tc:
                    log.warning("stale approval ignored: tool_call_id mismatch run=%s", run_id)
                    await self.manager.send_event(
                        conn_id,
                        AgentEvent(
                            "error",
                            {
                                "error": "审批响应与当前工具调用不匹配",
                                "fail_closed": True,
                                "reason_code": "stale_tool_call",
                            },
                            run_id,
                        ),
                    )
                    return
                decision = payload.get("decision", "deny")
                future.set_result("allow" if decision == "allow" else "deny")
            self._approval_futures.pop(run_id, None)
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
        interrupt_delivered = False
        dispatcher = RunEventDispatcher(run_id, maxsize=128)
        self._dispatchers[run_id] = dispatcher
        self._dispatch_conn[run_id] = conn_id
        dispatcher_task = asyncio.create_task(self._run_dispatcher(run_id, conn_id, dispatcher))

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
                    if event.type == "interrupt":
                        interrupt_delivered = True
                    if event.data.get("cancelled") and event.type in ("error", "done"):
                        cancel_notified = True
                    await self._send_run_event(run_id, event)

                if sid:
                    await self._handle_interrupts(conn_id, run_id, sid, already_notified=interrupt_delivered)

            except asyncio.CancelledError:
                status = "cancelled"
                if sid:
                    await self.chat_service.repair_cancelled_checkpoint(sid)
                    await self.chat_service.mark_terminal_checkpoint(sid, run_id, "cancelled", "cancelled")
                if not cancel_notified:
                    await self._emit_cancelled(conn_id, run_id, sid)
            except Exception as e:
                status = "failed"
                log.error("run.failed", error=str(e), exc_info=True)
                if sid:
                    await self.chat_service.mark_terminal_checkpoint(sid, run_id, "failed", "error")
                await self._send_run_event(run_id, AgentEvent("error", {"error": str(e)}, run_id))
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
                self._pending_interrupt_meta.pop(run_id, None)
                dispatcher.close()
                try:
                    await asyncio.shield(dispatcher_task)
                except asyncio.CancelledError:
                    pass
                self._dispatchers.pop(run_id, None)
                self._dispatch_conn.pop(run_id, None)

    async def _handle_interrupts(
        self, conn_id: int, run_id: str, session_id: str, *, already_notified: bool = False
    ) -> None:
        graph = await asyncio.to_thread(get_graph)
        config = make_thread_config(session_id)

        while await has_pending_interrupt(graph, config):
            interrupts = await get_pending_interrupts(graph, config)
            if not interrupts:
                break

            intr = interrupts[0]
            if isinstance(intr, dict):
                interrupt_data = dict(intr)
            else:
                interrupt_data = {"value": intr}

            # 规范化给客户端的字段
            value = interrupt_data.get("value") if "value" in interrupt_data else interrupt_data
            snapshot = await graph.aget_state(config)
            canonical = dict(snapshot.values or {}) if snapshot else {}
            scope = canonical.get("scope") or {}
            step_id = scope.get("step_id")
            if isinstance(value, dict):
                self._pending_interrupt_meta[run_id] = {
                    "kind": value.get("kind"),
                    "interrupt_id": value.get("interrupt_id"),
                    "tool_call_id": value.get("tool_call_id"),
                    "mode": value.get("mode"),
                    "session_id": session_id,
                    "run_id": run_id,
                    "step_id": step_id,
                }
                # 展平 value 便于前端直接读 kind/prompt
                event_data = {**value, "run_id": run_id, "session_id": session_id, "step_id": step_id}
            else:
                self._pending_interrupt_meta[run_id] = {
                    "session_id": session_id,
                    "run_id": run_id,
                    "step_id": step_id,
                }
                event_data = {**interrupt_data, "run_id": run_id, "session_id": session_id}

            if not already_notified:
                await self._send_run_event(
                    run_id,
                    AgentEvent(
                        "interrupt", event_data, run_id,
                        state=public_state_projection(canonical) if "context" in canonical else None,
                    ),
                )
            already_notified = False

            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            self._approval_futures[run_id] = future

            try:
                decision = await asyncio.wait_for(future, timeout=300.0)
            except TimeoutError:
                # fail-closed：超时视为拒绝 / 无回答
                kind = (self._pending_interrupt_meta.get(run_id) or {}).get("kind")
                if kind == "ask_user":
                    decision = {
                        "kind": "ask_user",
                        "mode": (self._pending_interrupt_meta.get(run_id) or {}).get("mode"),
                        "interrupt_id": (self._pending_interrupt_meta.get(run_id) or {}).get(
                            "interrupt_id"
                        ),
                        "answer": None,
                    }
                else:
                    decision = "deny"
                await self._send_run_event(run_id, AgentEvent("error", {"error": "等待用户响应超时，已安全拒绝"}, run_id))
            except asyncio.CancelledError:
                # cancel：fail-closed，不猜测答案
                kind = (self._pending_interrupt_meta.get(run_id) or {}).get("kind")
                if kind == "ask_user":
                    decision = {
                        "kind": "ask_user",
                        "mode": (self._pending_interrupt_meta.get(run_id) or {}).get("mode"),
                        "interrupt_id": (self._pending_interrupt_meta.get(run_id) or {}).get(
                            "interrupt_id"
                        ),
                        "answer": None,
                    }
                else:
                    decision = "deny"
                self._pending_interrupt_meta.pop(run_id, None)
                raise

            self._pending_interrupt_meta.pop(run_id, None)

            async for event in self.chat_service.resume_chat_events(
                session_id, decision, run_id=run_id
            ):
                await self._send_run_event(run_id, event)

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
        await self._send_run_event(run_id, AgentEvent("error", {"error": "已取消", "cancelled": True}, run_id))
        await self._send_run_event(
            run_id,
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
