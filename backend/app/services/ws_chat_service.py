from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from app.agent.approval_context import approval_enabled
from app.agent.state import public_state_projection, validate_agent_state
from app.agent.graph import get_graph, make_thread_config
from app.agent.streaming.events import AgentEvent
from app.agent.streaming.dispatcher import DispatchEvent, RunEventDispatcher
from app.agent.streaming.interrupts import get_pending_interrupts, has_pending_interrupt
from app.core.logging import get_logger, log_scope, new_trace_id
from app.services.chat_service import ChatService
from app.services.ws_manager import ConnectionManager
from app.schemas.chat import ChatRequest
from pydantic import ValidationError

log = get_logger(__name__)


class WsChatService:
    def __init__(self, manager: ConnectionManager, chat_service: ChatService | None = None):
        self.manager = manager
        self.chat_service = chat_service or ChatService()
        self._active_runs: dict[str, asyncio.Task] = {}
        self._approval_futures: dict[str, asyncio.Future] = {}
        self._run_sessions: dict[str, str] = {}
        self._run_branches: dict[str, str] = {}
        self._pending_interrupt_meta: dict[str, dict[str, Any]] = {}
        self._reattach_tasks: dict[str, asyncio.Task] = {}
        self._dispatchers: dict[str, RunEventDispatcher] = {}
        self._dispatch_conn: dict[str, int] = {}

    async def _send_run_event(self, turn_id: str, event: AgentEvent) -> bool:
        event.branch_id = event.branch_id or self._run_branches.get(turn_id)
        dispatcher = self._dispatchers.get(turn_id)
        if dispatcher is None:
            conn_id = self._dispatch_conn.get(turn_id)
            if conn_id is not None:
                await self.manager.send_event(conn_id, event)
            return False
        return await dispatcher.publish(
            event.type,
            {"agent_event": event},
            state=event if event.state is not None else None,
        )

    async def _run_dispatcher(self, turn_id: str, conn_id: int, dispatcher: RunEventDispatcher) -> None:
        async def send(item: DispatchEvent) -> None:
            value = item.payload.get("agent_event")
            if not isinstance(value, AgentEvent):
                return
            if item.kind == "state_sync":
                value = AgentEvent("state_sync", value.data, turn_id, state=value.state)
            await self.manager.send_event(self._dispatch_conn.get(turn_id, conn_id), value)

        await dispatcher.run(send)

    async def handle_message(self, conn_id: int, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        if "run_id" in payload:
            await self.manager.send_event(conn_id, AgentEvent(
                "error", {"error": "不支持旧版 run_id 协议，请更新客户端", "reason_code": "unsupported_protocol"}
            ))
            return
        if op == "ping":
            await self.manager.send_event(conn_id, AgentEvent("pong", {}))
            return

        if op == "session.subscribe":
            session_id = payload.get("session_id")
            self.manager.subscribe_session(conn_id, session_id)
            # A websocket can reconnect after the process persisted an interrupt.
            # Reattach the native waiting turn without creating a new user turn.
            if isinstance(session_id, str):
                await self._attach_waiting_turn(conn_id, session_id, payload.get("turn_id"),
                                                branch_id=payload.get("branch_id", "main"))
            return

        if op == "chat.send":
            turn_id = payload.get("turn_id") or "turn_" + uuid.uuid4().hex
            if not isinstance(turn_id, str) or not turn_id.strip():
                await self.manager.send_event(conn_id, AgentEvent("error", {"error": "turn_id 必须为非空字符串"}))
                return
            message = payload.get("message", "")
            session_id = payload.get("session_id")
            submission_id = payload.get("submission_id")
            if not isinstance(submission_id, str) or not submission_id.strip():
                await self.manager.send_event(conn_id, AgentEvent("error", {"error": "submission_id 必须为非空字符串", "reason_code": "invalid_submission_id"}, turn_id))
                return
            try:
                request = ChatRequest(message=message, session_id=session_id, turn_id=turn_id,
                                      submission_id=submission_id, branch_id=payload.get("branch_id", "main"),
                                      fork_cursor=payload.get("fork_cursor"))
            except ValidationError as exc:
                await self.manager.send_event(conn_id, AgentEvent("error", {
                    "error": str(exc), "reason_code": "invalid_branch_cursor",
                }, turn_id))
                return
            await self._start_run(conn_id, turn_id, message, session_id, submission_id,
                                  branch_id=request.branch_id,
                                  fork_cursor=request.fork_cursor.model_dump() if request.fork_cursor else None)
            return

        if op == "chat.cancel":
            turn_id = payload.get("turn_id")
            if (turn_id and self._run_sessions.get(turn_id) == payload.get("session_id")
                    and payload.get("branch_id", self._run_branches.get(turn_id, "main"))
                    == self._run_branches.get(turn_id, "main")):
                await self.cancel_run(turn_id)
            return

        if op == "state.sync":
            turn_id = payload.get("turn_id")
            session_id = payload.get("session_id")
            branch_id = payload.get("branch_id") or self._run_branches.get(turn_id, "main")
            if not turn_id or not session_id:
                return
            if self._run_sessions.get(turn_id) != session_id:
                await self._attach_waiting_turn(conn_id, session_id, turn_id, branch_id=branch_id)
            graph = await asyncio.to_thread(get_graph)
            snapshot = await graph.aget_state(make_thread_config(session_id, branch_id))
            canonical = dict(snapshot.values or {}) if snapshot else {}
            if canonical and (canonical.get("scope") or {}).get("turn_id") == turn_id and (canonical.get("scope") or {}).get("session_id") == session_id:
                validate_agent_state(canonical)
                active = self._active_runs.get(turn_id)
                if (active is None or active.done()) and hasattr(self.chat_service, "_recover_ledger_tail"):
                    canonical = self.chat_service._recover_ledger_tail(
                        session_id, canonical, **({"branch_id": branch_id} if branch_id != "main" else {}),
                    )
                if not isinstance(canonical, dict) or (canonical.get("scope") or {}).get("turn_id") != turn_id:
                    return
                event = AgentEvent(
                        "state_sync", {"session_id": session_id, "branch_id": branch_id}, turn_id,
                        state=public_state_projection(canonical),
                    )
                if turn_id in self._dispatchers:
                    await self._send_run_event(turn_id, event)
                else:
                    await self.manager.send_event(conn_id, event)
            return

        if op == "approve":
            turn_id = payload.get("turn_id")
            if not turn_id or turn_id not in self._approval_futures:
                if turn_id and payload.get("session_id"):
                    await self._attach_waiting_turn(conn_id, payload["session_id"], turn_id,
                                                    branch_id=payload.get("branch_id", "main"))
                if turn_id and turn_id not in self._approval_futures:
                    await self.manager.send_event(
                        conn_id,
                        AgentEvent(
                            "error",
                            {
                                "error": "审批或问答响应已过期",
                                "fail_closed": True,
                                "reason_code": "stale_interrupt",
                            },
                            turn_id,
                        ),
                    )
                if turn_id not in self._approval_futures:
                    return
            expected_session = self._run_sessions.get(turn_id)
            expected_branch = self._run_branches.get(turn_id, "main")
            if payload.get("branch_id", expected_branch) != expected_branch:
                await self.manager.send_event(conn_id, AgentEvent("error", {
                    "error": "响应与当前分支不匹配", "fail_closed": True, "reason_code": "stale_branch",
                }, turn_id))
                return
            payload_session = payload.get("session_id")
            if expected_session and payload_session != expected_session:
                log.warning(
                    "stale approval ignored: session mismatch run=%s",
                    turn_id,
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
                        turn_id,
                    ),
                )
                return

            pending = self._pending_interrupt_meta.get(turn_id) or {}
            expected_step = pending.get("step_id")
            provided_step = payload.get("step_id")
            if expected_step and provided_step != expected_step:
                await self.manager.send_event(
                    conn_id,
                    AgentEvent(
                        "error",
                        {"error": "响应与当前 step 不匹配", "fail_closed": True, "reason_code": "stale_step"},
                        turn_id,
                    ),
                )
                return
            future = self._approval_futures.get(turn_id)
            if future is None:
                return
            if future.done():
                await self.manager.send_event(conn_id, AgentEvent(
                    "error", {"error": "审批或问答响应已处理", "fail_closed": True, "reason_code": "stale_interrupt"}, turn_id,
                ))
                return

            expected_kind = pending.get("kind")
            requested_kind = payload.get("kind", "tool_approval")
            normalized_kind = "tool_approval" if expected_kind == "approval" else expected_kind
            if normalized_kind not in {"tool_approval", "ask_user"} or requested_kind != normalized_kind:
                await self.manager.send_event(conn_id, AgentEvent(
                    "error", {"error": "响应类型与当前交互不匹配", "fail_closed": True, "reason_code": "stale_interaction_kind"}, turn_id,
                ))
                return
            if not pending.get("interrupt_id") or payload.get("interrupt_id") != pending["interrupt_id"]:
                await self.manager.send_event(conn_id, AgentEvent(
                    "error", {"error": "响应与当前交互不匹配", "fail_closed": True, "reason_code": "stale_interrupt"}, turn_id,
                ))
                return

            if payload.get("kind") == "ask_user":
                interrupt_id = payload.get("interrupt_id")
                expected_iid = pending.get("interrupt_id")
                if expected_iid and interrupt_id != expected_iid:
                    log.warning("stale ask_user ignored: interrupt_id mismatch run=%s", turn_id)
                    await self.manager.send_event(
                        conn_id,
                        AgentEvent(
                            "error",
                            {
                                "error": "问答响应与当前中断不匹配",
                                "fail_closed": True,
                                "reason_code": "stale_interrupt",
                            },
                            turn_id,
                        ),
                    )
                    return
                tool_call_id = payload.get("tool_call_id")
                expected_tc = pending.get("tool_call_id")
                if expected_tc and tool_call_id != expected_tc:
                    log.warning("stale ask_user ignored: tool_call_id mismatch run=%s", turn_id)
                    await self.manager.send_event(
                        conn_id,
                        AgentEvent(
                            "error",
                            {
                                "error": "问答响应与当前工具调用不匹配",
                                "fail_closed": True,
                                "reason_code": "stale_tool_call",
                            },
                            turn_id,
                        ),
                    )
                    return
                mode = pending.get("mode") or "answer_and_continue"
                if payload.get("mode", mode) != mode:
                    await self.manager.send_event(conn_id, AgentEvent(
                        "error", {"error": "响应模式与当前问答不匹配", "fail_closed": True, "reason_code": "stale_interaction_mode"}, turn_id,
                    ))
                    return
                result: dict[str, Any] = {
                    "kind": "ask_user",
                    "mode": mode,
                    "interrupt_id": interrupt_id or expected_iid,
                    "tool_call_id": tool_call_id or expected_tc,
                    "step_id": expected_step,
                }
                if mode == "handoff_and_stop":
                    result["ack"] = True
                else:
                    result["answer"] = payload.get("answer")
                    from app.agent.nodes.tools import _format_ask_user_resume
                    valid_answer, _handoff = _format_ask_user_resume(
                        result, pending.get("options") or [], bool(pending.get("allow_multiple")),
                        mode=mode, interrupt_id=expected_iid or "",
                    )
                    if valid_answer is None:
                        await self.manager.send_event(conn_id, AgentEvent(
                            "error", {"error": "答案无效，请提供有效答案", "reason_code": "invalid_answer"}, turn_id,
                        ))
                        return
                future.set_result(result)
            else:
                if pending.get("interrupt_id") and payload.get("interrupt_id") != pending["interrupt_id"]:
                    await self.manager.send_event(conn_id, AgentEvent(
                        "error", {"error": "审批响应与当前交互不匹配", "reason_code": "stale_interrupt"}, turn_id,
                    ))
                    return
                # 工具审批：校验 tool_call_id
                tool_call_id = payload.get("tool_call_id")
                expected_tc = pending.get("tool_call_id")
                if expected_tc and tool_call_id != expected_tc:
                    log.warning("stale approval ignored: tool_call_id mismatch run=%s", turn_id)
                    await self.manager.send_event(
                        conn_id,
                        AgentEvent(
                            "error",
                            {
                                "error": "审批响应与当前工具调用不匹配",
                                "fail_closed": True,
                                "reason_code": "stale_tool_call",
                            },
                            turn_id,
                        ),
                    )
                    return
                decision = payload.get("decision", "deny")
                if decision not in {"allow", "deny"}:
                    await self.manager.send_event(conn_id, AgentEvent("error", {"error": "无效审批决定"}, turn_id))
                    return
                future.set_result({"kind": "tool_approval", "decision": decision,
                                   "interrupt_id": pending.get("interrupt_id"), "step_id": expected_step,
                                   "tool_call_id": expected_tc})
            return

        await self.manager.send_event(
            conn_id,
            AgentEvent("error", {"error": f"未知操作: {op}"}),
        )

    async def _start_run(
        self,
        conn_id: int,
        turn_id: str,
        message: str,
        session_id: str | None,
        submission_id: str,
        *,
        branch_id: str = "main",
        fork_cursor: dict[str, Any] | None = None,
    ) -> None:
        existing = self._active_runs.get(turn_id)
        if existing and not existing.done():
            await self.manager.send_event(
                conn_id,
                AgentEvent("error", {"error": "该 run 仍在执行中"}, turn_id),
            )
            return

        # Register an already known session before yielding to the new task, so
        # an immediately following cancellation can address this turn.
        if session_id:
            self._run_sessions[turn_id] = session_id
        self._run_branches[turn_id] = branch_id
        self._dispatch_conn[turn_id] = conn_id
        task = asyncio.create_task(self._execute_run(conn_id, turn_id, message, session_id, submission_id,
                                                      branch_id=branch_id, fork_cursor=fork_cursor))
        self._active_runs[turn_id] = task

    async def _attach_waiting_turn(
        self,
        conn_id: int,
        session_id: str,
        turn_id: str | None = None,
        *,
        start_wait: bool = True,
        branch_id: str = "main",
    ) -> bool:
        """Attach a websocket to a persisted native interrupt after reconnect.

        The checkpoint and ledger are authoritative. This path only registers an
        existing turn and never appends a user message or starts a new graph run.
        """
        try:
            graph = await asyncio.to_thread(get_graph)
            config = make_thread_config(session_id, branch_id)
            snapshot = await graph.aget_state(config)
            canonical = dict(snapshot.values or {}) if snapshot else {}
            if not canonical:
                return False
            validate_agent_state(canonical)
            active = self._active_runs.get((canonical.get("scope") or {}).get("turn_id"))
            if (active is None or active.done()) and hasattr(self.chat_service, "_recover_ledger_tail"):
                canonical = self.chat_service._recover_ledger_tail(
                    session_id, canonical, **({"branch_id": branch_id} if branch_id != "main" else {}),
                )
            scope = canonical.get("scope") or {}
            checkpoint_turn = scope.get("turn_id")
            if scope.get("session_id") != session_id or not checkpoint_turn or (turn_id and checkpoint_turn != turn_id):
                return False
            if canonical.get("turn_status") in {"completed", "failed", "cancelled", "handoff"}:
                return False
            pending = canonical.get("pending_interaction") or {}
            interrupts = await get_pending_interrupts(graph, config)
            value = interrupts[0] if interrupts else {}
            if isinstance(value, dict) and isinstance(value.get("value"), dict):
                value = value["value"]
            if not isinstance(value, dict) or not value or not isinstance(pending, dict) or not pending.get("id"):
                return False
            if value.get("interrupt_id") != pending["id"] or value.get("tool_call_id") != pending.get("call_id"):
                raise ValueError("persisted native interrupt does not match checkpoint interaction")
            pending = {**value, **pending, "options": value.get("options") or [], "allow_multiple": bool(value.get("allow_multiple"))}
            actual_turn = str(checkpoint_turn)
            self._run_sessions[actual_turn] = session_id
            self._run_branches[actual_turn] = branch_id
            self._dispatch_conn[actual_turn] = conn_id
            self._pending_interrupt_meta[actual_turn] = {
                **pending,
                "session_id": session_id,
                "turn_id": actual_turn,
                "branch_id": branch_id,
                "step_id": (canonical.get("current_step") or {}).get("step_id"),
                "tool_call_id": pending.get("call_id") or pending.get("tool_call_id"),
                "interrupt_id": pending.get("id"),
            }
            loop = asyncio.get_running_loop()
            future = self._approval_futures.get(actual_turn)
            if future is None:
                future = loop.create_future()
                self._approval_futures[actual_turn] = future
            if pending.get("decision") is not None and not future.done():
                future.set_result(pending["decision"])
            from app.storage.message_codec import safe_business_value
            if pending.get("decision") is None:
                await self.manager.send_event(conn_id, AgentEvent(
                    "interrupt", safe_business_value({**value, "session_id": session_id, "turn_id": actual_turn, "branch_id": branch_id}), actual_turn,
                    state=public_state_projection(canonical),
                ))
            active = self._active_runs.get(actual_turn)
            if start_wait and actual_turn not in self._reattach_tasks and (active is None or active.done()):
                task = asyncio.create_task(self._execute_waiting_turn(conn_id, actual_turn, session_id))
                self._reattach_tasks[actual_turn] = task
                self._active_runs[actual_turn] = task
                task.add_done_callback(lambda _task, key=actual_turn: self._reattach_tasks.pop(key, None))
            return True
        except Exception as exc:
            log.warning("failed to reattach persisted waiting turn", exc_info=True)
            await self.manager.send_event(conn_id, AgentEvent(
                "error", {"error": "无法恢复会话等待状态", "reason_code": "unsupported_checkpoint" if isinstance(exc, ValueError) else "resume_unavailable"}, turn_id,
            ))
            return False

    async def _execute_waiting_turn(self, conn_id: int, turn_id: str, session_id: str) -> None:
        branch_id = self._run_branches.get(turn_id, "main")
        token = approval_enabled.set(True)
        try:
            await self._handle_interrupts(conn_id, turn_id, session_id, already_notified=True)
        except asyncio.CancelledError:
            await self.chat_service.repair_cancelled_checkpoint(session_id, **({"branch_id": branch_id} if branch_id != "main" else {}))
            await self.chat_service.mark_terminal_checkpoint(session_id, turn_id, "cancelled", "cancelled", **({"branch_id": branch_id} if branch_id != "main" else {}))
            await self._emit_cancelled(conn_id, turn_id, session_id, branch_id=branch_id)
            raise
        except Exception:
            log.warning("persisted waiting turn resume failed", exc_info=True)
            await self.chat_service.mark_terminal_checkpoint(session_id, turn_id, "failed", "resume_error", **({"branch_id": branch_id} if branch_id != "main" else {}))
            await self._send_run_event(turn_id, AgentEvent("error", {"error": "恢复执行失败", "reason_code": "resume_error"}, turn_id))
        finally:
            approval_enabled.reset(token)
            self._active_runs.pop(turn_id, None)
            self._approval_futures.pop(turn_id, None)
            self._run_sessions.pop(turn_id, None)
            self._run_branches.pop(turn_id, None)
            self._pending_interrupt_meta.pop(turn_id, None)
            self._dispatch_conn.pop(turn_id, None)

    async def _execute_run(
        self,
        conn_id: int,
        turn_id: str,
        message: str,
        session_id: str | None,
        submission_id: str | None = None,
        *,
        branch_id: str = "main",
        fork_cursor: dict[str, Any] | None = None,
    ) -> None:
        start = time.monotonic()
        status = "success"
        sid: str | None = session_id
        self._run_branches[turn_id] = branch_id
        token = approval_enabled.set(True)
        cancel_notified = False
        interrupt_delivered = False
        dispatcher = RunEventDispatcher(turn_id, maxsize=128)
        self._dispatchers[turn_id] = dispatcher
        self._dispatch_conn[turn_id] = conn_id
        dispatcher_task = asyncio.create_task(self._run_dispatcher(turn_id, conn_id, dispatcher))

        with log_scope(
            trace_id=new_trace_id(),
            turn_id=turn_id,
            session_id=session_id,
            conn_id=conn_id,
        ):
            log.info("run.started")
            try:
                async for event in self.chat_service.iter_chat_events(
                    message, session_id, turn_id=turn_id, submission_id=submission_id,
                    branch_id=branch_id, fork_cursor=fork_cursor,
                ):
                    if event.type == "session" and event.data.get("session_id"):
                        sid = event.data["session_id"]
                        self._run_sessions[turn_id] = sid
                        self.manager.subscribe_session(conn_id, sid)
                    if event.type == "interrupt":
                        interrupt_delivered = True
                    if event.data.get("cancelled") and event.type in ("error", "done"):
                        cancel_notified = True
                    await self._send_run_event(turn_id, event)

                if sid:
                    await self._handle_interrupts(conn_id, turn_id, sid, already_notified=interrupt_delivered)

            except asyncio.CancelledError:
                status = "cancelled"
                if sid:
                    await self.chat_service.repair_cancelled_checkpoint(sid, **({"branch_id": branch_id} if branch_id != "main" else {}))
                    await self.chat_service.mark_terminal_checkpoint(sid, turn_id, "cancelled", "cancelled", **({"branch_id": branch_id} if branch_id != "main" else {}))
                if not cancel_notified:
                    await self._emit_cancelled(conn_id, turn_id, sid, branch_id=branch_id)
            except Exception as e:
                status = "failed"
                log.error("run.failed", error=str(e), exc_info=True)
                if sid:
                    await self.chat_service.mark_terminal_checkpoint(sid, turn_id, "failed", "error", **({"branch_id": branch_id} if branch_id != "main" else {}))
                await self._send_run_event(turn_id, AgentEvent("error", {"error": str(e)}, turn_id))
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
                self._active_runs.pop(turn_id, None)
                self._approval_futures.pop(turn_id, None)
                self._run_sessions.pop(turn_id, None)
                self._run_branches.pop(turn_id, None)
                self._pending_interrupt_meta.pop(turn_id, None)
                dispatcher.close()
                try:
                    await asyncio.shield(dispatcher_task)
                except asyncio.CancelledError:
                    pass
                self._dispatchers.pop(turn_id, None)
                self._dispatch_conn.pop(turn_id, None)

    async def _handle_interrupts(
        self, conn_id: int, turn_id: str, session_id: str, *, already_notified: bool = False
    ) -> None:
        graph = await asyncio.to_thread(get_graph)
        branch_id = self._run_branches.get(turn_id, "main")
        config = make_thread_config(session_id, branch_id)

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
            if canonical and hasattr(self.chat_service, "_recover_ledger_tail"):
                canonical = self.chat_service._recover_ledger_tail(
                    session_id, canonical, **({"branch_id": branch_id} if branch_id != "main" else {}),
                )
            if canonical and ((canonical.get("scope") or {}).get("turn_id") != turn_id or canonical.get("turn_status") in {"completed", "failed", "cancelled", "handoff"}):
                return
            scope = canonical.get("scope") or {}
            step_id = (canonical.get("current_step") or {}).get("step_id")
            pending = canonical.get("pending_interaction") or {}
            if isinstance(value, dict):
                self._pending_interrupt_meta[turn_id] = {
                    "kind": pending.get("kind") or value.get("kind"),
                    "interrupt_id": pending.get("id") or value.get("interrupt_id"),
                    "tool_call_id": pending.get("call_id") or value.get("tool_call_id"),
                    "mode": pending.get("mode") or value.get("mode"),
                    "options": pending.get("options", value.get("options")),
                    "allow_multiple": pending.get("allow_multiple", value.get("allow_multiple")),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "branch_id": branch_id,
                    "step_id": step_id,
                }
                # 展平 value 便于前端直接读 kind/prompt
                event_data = {**value, "turn_id": turn_id, "session_id": session_id, "branch_id": branch_id, "step_id": step_id,
                              "interrupt_id": pending.get("id") or value.get("interrupt_id")}
            else:
                self._pending_interrupt_meta[turn_id] = {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "step_id": step_id,
                }
                event_data = {**interrupt_data, "turn_id": turn_id, "session_id": session_id, "branch_id": branch_id}

            if not already_notified:
                from app.storage.message_codec import safe_business_value
                await self._send_run_event(
                    turn_id,
                    AgentEvent(
                        "interrupt", safe_business_value(event_data), turn_id,
                        state=public_state_projection(canonical) if "context" in canonical else None,
                    ),
                )
            already_notified = False

            loop = asyncio.get_running_loop()
            future = self._approval_futures.get(turn_id)
            if future is None:
                future = loop.create_future()
                self._approval_futures[turn_id] = future

            try:
                decision = await asyncio.wait_for(future, timeout=300.0)
            except TimeoutError:
                # fail-closed：超时视为拒绝 / 无回答
                kind = (self._pending_interrupt_meta.get(turn_id) or {}).get("kind")
                if kind == "ask_user":
                    decision = {
                        "kind": "ask_user",
                        "mode": (self._pending_interrupt_meta.get(turn_id) or {}).get("mode"),
                        "interrupt_id": (self._pending_interrupt_meta.get(turn_id) or {}).get(
                            "interrupt_id"
                        ),
                        "answer": None,
                        "source": "timeout",
                        "step_id": (self._pending_interrupt_meta.get(turn_id) or {}).get("step_id"),
                        "tool_call_id": (self._pending_interrupt_meta.get(turn_id) or {}).get("tool_call_id"),
                    }
                else:
                    meta = self._pending_interrupt_meta.get(turn_id) or {}
                    decision = {"kind": "tool_approval", "decision": "deny", "source": "timeout", "interrupt_id": meta.get("interrupt_id"),
                                "step_id": meta.get("step_id"), "tool_call_id": meta.get("tool_call_id")}
                await self._send_run_event(turn_id, AgentEvent("stage", {
                    "stage": "interaction_timeout", "status": "completed",
                    "reason_code": "interaction_timeout", "error": "等待用户响应超时，已安全拒绝",
                }, turn_id))
            except asyncio.CancelledError:
                # cancel：fail-closed，不猜测答案
                kind = (self._pending_interrupt_meta.get(turn_id) or {}).get("kind")
                if kind == "ask_user":
                    decision = {
                        "kind": "ask_user",
                        "mode": (self._pending_interrupt_meta.get(turn_id) or {}).get("mode"),
                        "interrupt_id": (self._pending_interrupt_meta.get(turn_id) or {}).get(
                            "interrupt_id"
                        ),
                        "answer": None,
                    }
                else:
                    decision = "deny"
                self._pending_interrupt_meta.pop(turn_id, None)
                raise

            self._pending_interrupt_meta.pop(turn_id, None)
            self._approval_futures.pop(turn_id, None)

            async for event in self.chat_service.resume_chat_events(
                session_id, decision, turn_id=turn_id,
                **({"branch_id": branch_id} if branch_id != "main" else {}),
            ):
                await self._send_run_event(turn_id, event)

            graph = await asyncio.to_thread(get_graph)

    async def cancel_run(self, turn_id: str) -> None:
        session_id = self._run_sessions.get(turn_id)
        branch_id = self._run_branches.get(turn_id, "main")
        future = self._approval_futures.pop(turn_id, None)
        if future and not future.done():
            future.cancel()

        task = self._active_runs.get(turn_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if session_id:
            from app.tools.packages.shell.process_registry import kill_session_processes

            kill_session_processes(session_id)
            await self.chat_service.repair_cancelled_checkpoint(session_id, **({"branch_id": branch_id} if branch_id != "main" else {}))
            await self.chat_service.mark_terminal_checkpoint(session_id, turn_id, "cancelled", "cancelled", **({"branch_id": branch_id} if branch_id != "main" else {}))
        # Cancellation may happen before a newly created coroutine runs its
        # own finally block, including before a new session has been created.
        if turn_id in self._dispatch_conn:
            await self._emit_cancelled(self._dispatch_conn[turn_id], turn_id, session_id, branch_id=branch_id)
            self._dispatch_conn.pop(turn_id, None)
        self._active_runs.pop(turn_id, None)
        self._run_sessions.pop(turn_id, None)
        self._run_branches.pop(turn_id, None)
        self._pending_interrupt_meta.pop(turn_id, None)

    async def _emit_cancelled(
        self,
        conn_id: int,
        turn_id: str,
        session_id: str | None,
        *,
        branch_id: str = "main",
    ) -> None:
        """取消时推送成对终态事件，便于前端清理工具/loading 状态。"""
        await self._send_run_event(turn_id, AgentEvent("error", {"error": "已取消", "cancelled": True}, turn_id,
                                                     branch_id=branch_id))
        await self._send_run_event(
            turn_id,
            AgentEvent(
                "done",
                {
                    "content": "",
                    "session_id": session_id,
                    "branch_id": branch_id,
                    "partial": True,
                    "cancelled": True,
                },
                turn_id,
                branch_id=branch_id,
            ),
        )
