import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage, messages_from_dict
from langgraph.graph.message import add_messages

from app.agent.messages import is_ephemeral_prompt_frame, is_internal_prompt_message
from app.agent.graph import get_graph, make_initial_state, make_thread_config
from app.agent.state import REQUIRED_TOP_LEVEL, to_runtime_view, merge_runtime_updates, to_graph_state, terminal_patch
from app.agent.hooks import (
    ON_SESSION_END,
    ON_SESSION_START,
    dispatch_observe,
    dispatch_post_llm_call,
)
from app.agent.streaming.events import AgentEvent, to_sse
from app.agent.streaming.sse_mapper import extract_final_content, stream_graph_events
from app.context.engine import resolve_context_usage
from app.context.ledger import repair_orphan_tool_calls
from app.context.memory.maintenance import prepare_session_memory
from app.context.tool_cache import save_tool_result, summarize_tool_result
from app.core.latency import (
    LATENCY_STAGE_ACCEPTED,
    LATENCY_STAGE_CONTEXT_PREPARE,
    LATENCY_STAGE_HISTORY_LOAD,
    LATENCY_STAGE_MODEL_STREAM,
    LATENCY_STAGE_POSTPROCESS,
    RunLatency,
)
from app.maintenance.runner import get_maintenance_runner
from app.core.llm import get_chat_model, load_agent_config
from app.core.settings import get_settings
from app.resilience import GRAPH_REQUEST_TIMEOUT_SECONDS
from app.schemas.chat import ChatResponse
from app.schemas.session import (
    ChatMessage,
    Session,
    SessionHistory,
    SessionHistoryPage,
    ToolCall,
    ToolCallFunction,
    ToolResultPayload,
)
from app.core.exceptions import AppError
from app.storage.history_projection import HistoryProjectionStore
from app.storage.conversation_files import SessionConversationFiles
from app.storage.project import require_bound_session, session_project_path
from app.storage.workspace import SessionStore, project_tool_results_dir
from app.tools.registry import load_full_config
from app.tools.tool_search import resolve_tool_display, should_show_tool_in_ui

log = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = 30
MAX_HISTORY_LIMIT = 200
PREPARE_MEMORY_TIMEOUT_SECONDS = 8.0


class ChatService:
    def __init__(self):
        self.settings = get_settings()
        self.session_store = SessionStore(self.settings.workspace_path)
        self.history_projection = HistoryProjectionStore(self.settings.workspace_path)
        self.conversation_files = SessionConversationFiles(self.settings.workspace_path)

    def _new_run_state(
        self, session_id: str, message: str, max_steps: int, run_id: str | None,
        previous: dict[str, Any] | None = None,
    ):
        state = make_initial_state(
            message, max_steps, session_id=session_id,
            workspace_id=str(self.settings.workspace_path), run_id=run_id,
        )
        if previous and REQUIRED_TOP_LEVEL.issubset(set(previous) | {"checkpoint_id"}):
            previous_context = dict(previous.get("context") or {})
            previous_memory = dict(previous.get("memory") or {})
            prior_messages = [
                msg for msg in (previous_context.get("working_message") or [])
                if not is_ephemeral_prompt_frame(msg)
            ]
            state["context"].update({key: value for key, value in previous_context.items() if key != "working_message"})
            state["context"]["working_message"] = [*prior_messages, *state["context"]["working_message"]]
            state["memory"].update(previous_memory)
            state["tasks"] = list(dict.fromkeys([*(previous.get("tasks") or []), *state["tasks"]]))
            state["task_details"] = {**(previous.get("task_details") or {}), **state["task_details"]}
        state["context"]["bootstrap_fingerprint"] = self._bootstrap_fingerprint(session_id)
        seq = self.conversation_files.append_messages(
            session_id,
            str(state["scope"]["run_id"]),
            [state["context"]["working_message"][-1]],
            scope=state["scope"],
        )
        state["ledger_pointer"]["applied_seq"] = seq
        state["ledger_pointer"]["applied_offset"] = self.conversation_files.offset_after(session_id, seq)
        return state

    def _bootstrap_fingerprint(self, session_id: str) -> str:
        from app.context.bootstrap import load_bootstrap

        project = session_project_path(session_id)
        text, _tokens = load_bootstrap(self.settings.workspace_path, Path(project) if project else None)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _recover_ledger_tail(self, session_id: str, previous: dict[str, Any]) -> dict[str, Any]:
        pointer = dict(previous.get("ledger_pointer") or {})
        if self.conversation_files.has_sequence_anomalies(session_id):
            rows = self.conversation_files.read(session_id)
            recovered = dict(previous)
            context = dict(previous.get("context") or {})
            messages = messages_from_dict([row["message"] for row in rows])
            context["working_message"] = [
                msg for msg in messages if not is_ephemeral_prompt_frame(msg)
            ]
            recovered["context"] = context
            latest_seq = self.conversation_files.latest_seq(session_id)
            recovered["ledger_pointer"] = {
                **pointer,
                "applied_seq": latest_seq,
                "applied_offset": self.conversation_files.offset_after(session_id, latest_seq),
            }
            return recovered
        rows = self.conversation_files.read_tail(
            session_id,
            after_seq=int(pointer.get("applied_seq", 0)),
            offset=pointer.get("applied_offset"),
        )
        if not rows:
            return previous
        recovered = dict(previous)
        context = dict(previous.get("context") or {})
        messages = messages_from_dict([row["message"] for row in rows])
        existing = list(context.get("working_message") or [])
        seen_ids = {
            (message.additional_kwargs or {}).get("ledger_message_id")
            for message in existing if (message.additional_kwargs or {}).get("ledger_message_id")
        }
        unseen = [message for message in messages if (message.additional_kwargs or {}).get("ledger_message_id") not in seen_ids]
        context["working_message"] = add_messages(existing, unseen)
        recovered["context"] = context
        recovered["ledger_pointer"] = {
            **pointer,
            "applied_seq": int(rows[-1]["seq"]),
            "applied_offset": int(rows[-1]["_end_offset"]),
        }
        return recovered

    def _refresh_stale_context(self, session_id: str, previous: dict[str, Any]) -> dict[str, Any]:
        context = dict(previous.get("context") or {})
        fingerprint = self._bootstrap_fingerprint(session_id)
        if context.get("bootstrap_fingerprint") == fingerprint:
            return previous
        # Bootstrap/reference changes invalidate the prepared window. Rebuild it
        # from the durable ledger once, then let the existing compression pipeline
        # establish a fresh token-bounded working window before the next model call.
        rows = self.conversation_files.read(session_id)
        rebuilt = dict(previous)
        context["working_message"] = [
            msg for msg in messages_from_dict([row["message"] for row in rows])
            if not is_ephemeral_prompt_frame(msg)
        ]
        context["bootstrap_fingerprint"] = fingerprint
        context["last_built_at"] = None
        rebuilt["context"] = context
        if rows:
            pointer = dict(previous.get("ledger_pointer") or {})
            pointer["applied_seq"] = int(rows[-1]["seq"])
            pointer["applied_offset"] = self.conversation_files.offset_after(session_id, int(rows[-1]["seq"]))
            rebuilt["ledger_pointer"] = pointer
        return rebuilt

    async def _checkpoint_values(self, graph, config: dict) -> dict[str, Any]:
        if not hasattr(graph, "aget_state"):
            return {}
        snapshot = await graph.aget_state(config)
        return dict(snapshot.values or {}) if snapshot else {}

    async def _reject_legacy_checkpoint(self, graph, config: dict) -> None:
        if not hasattr(graph, "aget_state"):
            return
        snapshot = await graph.aget_state(config)
        values = dict(snapshot.values or {}) if snapshot else {}
        if not values:
            return
        required = REQUIRED_TOP_LEVEL - {"checkpoint_id"}
        legacy_shape = {"meta", "session", "data", "task"}.issubset(values)
        if legacy_shape and (
            not required.issubset(values) or not ("checkpoint_id" in values or "checkpoint_uid" in values)
        ):
            raise RuntimeError("不支持旧版 Agent checkpoint；请新建 session 后继续对话")

    def _max_steps(self) -> int:
        from app.agent.iteration_budget import DEFAULT_MAX_STEPS, clamp_max_steps

        config = load_full_config(self.settings.workspace_path)
        return clamp_max_steps(
            config.get("agent", {}).get("max_steps"),
            default=DEFAULT_MAX_STEPS,
        )

    def _require_bound_session(self, session_id: str | None) -> str:
        sid, _project = require_bound_session(session_id)
        return sid

    def _schedule_background(self, name: str, factory) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _runner() -> None:
            try:
                await factory()
            except Exception:
                log.warning("%s failed", name, exc_info=True)

        loop.create_task(_runner())

    async def _prepare_memory_bounded(self, message: str, latency: RunLatency) -> str:
        try:
            await asyncio.wait_for(
                prepare_session_memory(message),
                timeout=PREPARE_MEMORY_TIMEOUT_SECONDS,
            )
            return "done"
        except TimeoutError:
            log.warning(
                "memory preparation timed out",
                extra=latency.summary(status="degraded", failed_stage=LATENCY_STAGE_CONTEXT_PREPARE),
            )
            return "degraded"
        except Exception:
            log.warning("memory preparation failed", exc_info=True)
            return "failed"

    async def _get_graph_bounded(self):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(get_graph),
                timeout=min(10.0, GRAPH_REQUEST_TIMEOUT_SECONDS),
            )
        except TimeoutError as exc:
            raise RuntimeError("Agent 图准备超时，请稍后重试") from exc

    def _history_from_values(
        self,
        session_id: str,
        values: dict,
        *,
        compact_tools: bool,
    ) -> SessionHistory:
        projected = to_runtime_view(values) if "context" in values else values
        ledger_rows = self.conversation_files.read(session_id)
        ledger_messages = messages_from_dict([row["message"] for row in ledger_rows])
        messages = repair_orphan_tool_calls(ledger_messages or projected.get("messages", []))
        session_token_stats = projected.get("session_token_stats") or {}
        api_usage = projected.get("api_usage") or {}
        context_usage = resolve_context_usage({**projected, "messages": messages}, session_id)
        return SessionHistory(
            session_id=session_id,
            messages=self.messages_to_openai(
                messages,
                session_id=session_id,
                compact_tools=compact_tools,
                ledger_rows=ledger_rows,
            ),
            context_usage=context_usage,
            api_usage=api_usage or None,
            session_token_stats=session_token_stats or None,
            todos=list(projected.get("todos") or []) or None,
        )

    async def _history_from_checkpoint(
        self,
        session_id: str,
        *,
        compact_tools: bool,
    ) -> SessionHistory:
        self.session_store.get(session_id)
        graph = await self._get_graph_bounded()
        config = make_thread_config(session_id)
        state = await graph.aget_state(config)
        values = dict(state.values) if state and state.values else {}
        return self._history_from_values(session_id, values, compact_tools=compact_tools)

    async def _refresh_history_projection(
        self,
        session_id: str,
        *,
        graph=None,
        config: dict | None = None,
    ) -> None:
        try:
            graph = graph or await self._get_graph_bounded()
            config = config or make_thread_config(session_id)
            state = await graph.aget_state(config)
            values = dict(state.values) if state and state.values else {}
            history = self._history_from_values(session_id, values, compact_tools=True)
            self.history_projection.write(
                session_id,
                history.messages,
                context_usage=history.context_usage,
                api_usage=history.api_usage,
                session_token_stats=history.session_token_stats,
                todos=history.todos,
                ledger_seq=self.conversation_files.latest_seq(session_id),
            )
        except Exception:
            log.warning("history projection refresh failed", exc_info=True)

    async def get_history_page(
        self,
        session_id: str,
        *,
        limit: int = DEFAULT_HISTORY_LIMIT,
        before: str | None = None,
    ) -> SessionHistoryPage:
        latency = RunLatency(run_id=None, session_id=session_id)
        latency.start(LATENCY_STAGE_HISTORY_LOAD)
        meta = self.session_store.get(session_id)
        limit = max(1, min(MAX_HISTORY_LIMIT, int(limit or DEFAULT_HISTORY_LIMIT)))

        payload = self.history_projection.read(session_id)
        if payload is None or int(payload.get("ledger_seq", -1)) < self.conversation_files.latest_seq(session_id):
            ledger_rows = self.conversation_files.read(session_id)
            ledger_messages = messages_from_dict([row["message"] for row in ledger_rows])
            if ledger_messages:
                history = SessionHistory(
                    session_id=session_id,
                    messages=self.messages_to_openai(
                        repair_orphan_tool_calls(ledger_messages),
                        session_id=session_id,
                        compact_tools=True,
                        ledger_rows=ledger_rows,
                    ),
                )
            else:
                history = await self._history_from_checkpoint(session_id, compact_tools=True)
            self.history_projection.write(
                session_id,
                history.messages,
                context_usage=history.context_usage,
                api_usage=history.api_usage,
                session_token_stats=history.session_token_stats,
                todos=history.todos,
                ledger_seq=self.conversation_files.latest_seq(session_id),
            )
            payload = self.history_projection.read(session_id) or {
                "messages": [m.model_dump(mode="json") for m in history.messages],
                "context_usage": history.context_usage,
                "api_usage": history.api_usage,
                "session_token_stats": history.session_token_stats,
                "todos": history.todos,
            }

        page_messages, has_more, next_cursor = self.history_projection.page(
            payload,
            limit=limit,
            before=before,
        )
        latency.finish(LATENCY_STAGE_HISTORY_LOAD)
        log.info(
            "history.load.completed",
            extra=latency.summary(
                status="success",
                message_count=len(page_messages),
                has_more=has_more,
            ),
        )
        return SessionHistoryPage(
            session_id=session_id,
            session=Session(**meta),
            messages=page_messages,
            context_usage=payload.get("context_usage"),
            api_usage=payload.get("api_usage"),
            session_token_stats=payload.get("session_token_stats"),
            todos=payload.get("todos"),
            limit=limit,
            before=before,
            has_more=has_more,
            next_cursor=next_cursor,
            projection_updated_at=payload.get("updated_at"),
        )

    async def get_tool_result(self, session_id: str, tool_call_id: str) -> ToolResultPayload:
        self.session_store.get(session_id)
        project = session_project_path(session_id)
        if project is None:
            return ToolResultPayload(
                session_id=session_id,
                tool_call_id=tool_call_id,
                available=False,
                error="会话未绑定项目工作区",
            )
        safe_id = re.sub(r"[^\w\-]", "_", tool_call_id or "unknown")
        path = project_tool_results_dir(Path(project), session_id) / f"{safe_id}.json"
        if not path.exists():
            return ToolResultPayload(
                session_id=session_id,
                tool_call_id=tool_call_id,
                content=None,
                available=False,
                error="工具结果不可用或尚未落盘",
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            content = payload.get("content")
            if not isinstance(content, str):
                raise ValueError("工具结果格式无效")
            return ToolResultPayload(session_id=session_id, tool_call_id=tool_call_id, content=content)
        except Exception as exc:
            return ToolResultPayload(
                session_id=session_id,
                tool_call_id=tool_call_id,
                content=None,
                available=False,
                error=str(exc),
            )

    async def _llm_short_title(self, user_message: str, assistant_content: str) -> str | None:
        if not self.settings.has_llm:
            return None
        prompt = (
            "把这次对话总结成不超过16个汉字的短标题。"
            "只输出标题本身，不要引号、句号或解释。\n"
            f"用户：{(user_message or '')[:200]}\n"
            f"助手：{(assistant_content or '')[:200]}"
        )
        try:
            model = get_chat_model()
            result = await asyncio.wait_for(
                model.ainvoke([HumanMessage(content=prompt)]),
                timeout=8,
            )
            raw = result.content if hasattr(result, "content") else str(result)
            if isinstance(raw, list):
                raw = "".join(str(part) for part in raw)
            text = str(raw or "").strip().splitlines()[0].strip().strip("「」\"'")
            return text or None
        except Exception:
            return None

    async def _refine_auto_title_after_turn(
        self,
        session_id: str,
        user_message: str,
        assistant_content: str,
    ) -> None:
        try:
            meta = self.session_store.get(session_id)
            if meta.get("title_source") != "auto":
                return
            if not (assistant_content or "").strip():
                return
            summary = await self._llm_short_title(user_message, assistant_content)
            if summary:
                self.session_store.apply_auto_title(session_id, summary)
        except Exception:
            return

    async def send_sync(
        self,
        message: str,
        session_id: str | None,
        *,
        run_id: str | None = None,
    ) -> ChatResponse:
        from app.core.settings import get_settings

        latency = RunLatency(run_id=run_id, session_id=session_id)
        sid = self._require_bound_session(session_id)
        latency.bind_session(sid)

        if not get_settings().has_llm:
            raise RuntimeError("未配置 LLM API Key")
        self.session_store.maybe_set_title_from_message(sid, message)
        latency.start(LATENCY_STAGE_CONTEXT_PREPARE)
        await self._prepare_memory_bounded(message, latency)
        graph = await self._get_graph_bounded()
        config = make_thread_config(sid)
        await self._reject_legacy_checkpoint(graph, config)
        previous = self._refresh_stale_context(
            sid, self._recover_ledger_tail(sid, await self._checkpoint_values(graph, config))
        )
        max_steps = self._max_steps()
        state = self._new_run_state(sid, message, max_steps, run_id, previous)
        run_id = str(state["scope"]["run_id"])
        latency.finish(LATENCY_STAGE_CONTEXT_PREPARE)

        try:
            latency.start(LATENCY_STAGE_MODEL_STREAM)
            result = await asyncio.wait_for(
                graph.ainvoke(to_graph_state(state), config),
                timeout=GRAPH_REQUEST_TIMEOUT_SECONDS,
            )
            latency.finish(LATENCY_STAGE_MODEL_STREAM)
        except TimeoutError as exc:
            latency.finish(LATENCY_STAGE_MODEL_STREAM, "failed")
            raise RuntimeError(
                f"请求超时（{int(GRAPH_REQUEST_TIMEOUT_SECONDS)}s），请稍后重试"
            ) from exc
        projected_result = to_runtime_view(result)
        content = extract_final_content(projected_result.get("messages", []))
        self.session_store.touch(sid)
        await self._refresh_history_projection(sid, graph=graph, config=config)
        self._schedule_background(
            "auto-title refinement",
            lambda: self._refine_auto_title_after_turn(sid, message, content),
        )
        dispatch_post_llm_call(
            session_id=sid,
            user_message=message,
            assistant_response=content,
        )
        dispatch_observe(ON_SESSION_END, session_id=sid, completed=True, interrupted=False)
        if projected_result.get("memory_nudge_pending"):
            self._schedule_background(
                "memory nudge",
                lambda: get_maintenance_runner().run_memory_nudge_for_session(sid),
            )
        log.info("chat.run.completed", extra=latency.summary(status="success"))
        return ChatResponse(content=content, session_id=sid)

    async def iter_chat_events(
        self,
        message: str,
        session_id: str | None,
        *,
        run_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """聊天主流程：产出统一 AgentEvent 流（SSE / WS 共用）。"""
        latency = RunLatency(run_id=run_id, session_id=session_id)
        try:
            sid = self._require_bound_session(session_id)
        except AppError as exc:
            yield AgentEvent("error", {"error": exc.message}, run_id)
            return
        latency.bind_session(sid)

        if not self.settings.has_llm:
            yield AgentEvent("error", {"error": "未配置 LLM API Key，请在 backend/.env 设置 OPENAI_API_KEY"}, run_id)
            return
        yield AgentEvent("session", {"session_id": sid}, run_id)
        yield AgentEvent("stage", latency.instant(LATENCY_STAGE_ACCEPTED), run_id)
        self.session_store.maybe_set_title_from_message(sid, message)

        yield AgentEvent("stage", latency.start(LATENCY_STAGE_CONTEXT_PREPARE), run_id)
        memory_status = await self._prepare_memory_bounded(message, latency)
        try:
            graph = await self._get_graph_bounded()
        except Exception as exc:
            yield AgentEvent(
                "stage",
                latency.finish(LATENCY_STAGE_CONTEXT_PREPARE, "failed", error=str(exc)),
                run_id,
            )
            yield AgentEvent("error", {"error": str(exc)}, run_id)
            log.info("chat.run.completed", extra=latency.summary(status="failed", failed_stage=LATENCY_STAGE_CONTEXT_PREPARE))
            return
        config = make_thread_config(sid)
        try:
            await self._reject_legacy_checkpoint(graph, config)
        except RuntimeError as exc:
            yield AgentEvent("error", {"error": str(exc), "reason_code": "unsupported_checkpoint"}, run_id)
            return
        previous = self._refresh_stale_context(
            sid, self._recover_ledger_tail(sid, await self._checkpoint_values(graph, config))
        )
        max_steps = self._max_steps()
        state = self._new_run_state(sid, message, max_steps, run_id, previous)
        run_id = str(state["scope"]["run_id"])
        yield AgentEvent(
            "stage",
            latency.finish(LATENCY_STAGE_CONTEXT_PREPARE, memory_status),
            run_id,
        )
        yield AgentEvent("stage", latency.start(LATENCY_STAGE_MODEL_STREAM), run_id)

        nudge_pending = False
        final_content = ""
        completed = False
        async for event in stream_graph_events(graph, state, config, max_steps, run_id=run_id):
            if event.type == "done":
                self.session_store.touch(sid)
                final_content = event.data.get("content", "") or ""
                completed = not event.data.get("partial", False)
                snapshot = await graph.aget_state(config)
                values = dict(snapshot.values) if snapshot and snapshot.values else {}
                projected_values = to_runtime_view(values) if "context" in values else values
                nudge_pending = bool(projected_values.get("memory_nudge_pending"))
                yield AgentEvent(
                    "stage",
                    latency.finish(LATENCY_STAGE_MODEL_STREAM, "done" if completed else "partial"),
                    run_id,
                )
            elif event.type == "error":
                yield AgentEvent(
                    "stage",
                    latency.finish(LATENCY_STAGE_MODEL_STREAM, "failed"),
                    run_id,
                )
            yield event

        if completed:
            self._schedule_background(
                "auto-title refinement",
                lambda: self._refine_auto_title_after_turn(sid, message, final_content),
            )
            self._schedule_background(
                "history projection refresh",
                lambda: self._refresh_history_projection(sid, graph=graph, config=config),
            )
            dispatch_post_llm_call(
                session_id=sid,
                user_message=message,
                assistant_response=final_content,
            )
        dispatch_observe(
            ON_SESSION_END,
            session_id=sid,
            completed=completed,
            interrupted=not completed,
        )

        if nudge_pending:
            yield AgentEvent(
                "stage",
                latency.instant(LATENCY_STAGE_POSTPROCESS, status="background"),
                run_id,
            )
            self._schedule_background(
                "memory nudge",
                lambda: get_maintenance_runner().run_memory_nudge_for_session(sid),
            )
        log.info("chat.run.completed", extra=latency.summary(status="success" if completed else "partial"))

    async def send_stream(
        self,
        message: str,
        session_id: str | None,
        *,
        run_id: str | None = None,
    ) -> AsyncIterator[str]:
        from app.agent.streaming.interrupts import has_pending_interrupt

        sid: str | None = session_id
        async for event in self.iter_chat_events(message, session_id, run_id=run_id):
            if event.type == "session" and event.data.get("session_id"):
                sid = event.data["session_id"]
            yield to_sse(event)

        if not sid:
            return
        graph = await self._get_graph_bounded()
        if await has_pending_interrupt(graph, make_thread_config(sid)):
            yield to_sse(
                AgentEvent(
                    "error",
                    {
                        "error": "此操作需要在 WebSocket 连接下确认或回答，请重连后重试",
                        "fail_closed": True,
                        "reason_code": "sse_no_answer_channel",
                    },
                    run_id,
                )
            )

    async def resume_chat_events(
        self,
        session_id: str,
        decision: Any,
        *,
        run_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """恢复 interrupt 后继续产出事件流。"""
        from langgraph.types import Command

        graph = await self._get_graph_bounded()
        config = make_thread_config(session_id)
        await self._reject_legacy_checkpoint(graph, config)
        values = await self._checkpoint_values(graph, config)
        checkpoint_run_id = (values.get("scope") or {}).get("run_id")
        if run_id and checkpoint_run_id and run_id != checkpoint_run_id:
            raise RuntimeError("恢复请求与 checkpoint 中的 run 不匹配")
        run_id = str(checkpoint_run_id or run_id or "")
        pending = values.get("pending_interaction") if values else None
        if isinstance(pending, dict):
            response_id = decision.get("interrupt_id") if isinstance(decision, dict) else None
            if response_id and pending.get("interrupt_id") and response_id != pending.get("interrupt_id"):
                raise RuntimeError("响应与 checkpoint 中的 interrupt 不匹配")
            if hasattr(graph, "aupdate_state"):
                await graph.aupdate_state(
                    config,
                    {
                        "state_revision": int(values.get("state_revision", 0)) + 1,
                        "checkpoint_uid": f"cp_{uuid.uuid4().hex}",
                        "run_status": "running",
                        "current_step": {**values.get("current_step", {}), "step_status": "running"},
                        "pending_interaction": None,
                    },
                )
        max_steps = self._max_steps()
        latency = RunLatency(run_id=run_id, session_id=session_id)
        yield AgentEvent("stage", latency.start(LATENCY_STAGE_MODEL_STREAM), run_id)

        async for event in stream_graph_events(
            graph,
            {},
            config,
            max_steps,
            run_id=run_id,
            input_override=Command(resume=decision),
        ):
            if event.type == "done":
                self.session_store.touch(session_id)
                yield AgentEvent("stage", latency.finish(LATENCY_STAGE_MODEL_STREAM), run_id)
                self._schedule_background(
                    "history projection refresh",
                    lambda: self._refresh_history_projection(session_id, graph=graph, config=config),
                )
            elif event.type == "error":
                yield AgentEvent("stage", latency.finish(LATENCY_STAGE_MODEL_STREAM, "failed"), run_id)
            yield event

    async def repair_cancelled_checkpoint(self, session_id: str) -> None:
        """取消时修复未完成的 tool_calls。"""
        from app.context.ledger import cancel_tool_calls_message, has_pending_tool_calls

        graph = await self._get_graph_bounded()
        config = make_thread_config(session_id)
        snapshot = await graph.aget_state(config)
        if not snapshot or not snapshot.values:
            return
        projected = to_runtime_view(dict(snapshot.values or {})) if "context" in snapshot.values else dict(snapshot.values or {})
        messages = list(projected.get("messages") or [])
        if not has_pending_tool_calls(messages):
            return
        last = messages[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            cancelled = cancel_tool_calls_message(last)
            values = dict(snapshot.values or {})
            scope = values.get("scope") or {}
            run_id = str(scope.get("run_id") or "")
            step_id = str((values.get("current_step") or {}).get("step_id") or "")
            seq = self.conversation_files.append_messages(
                session_id, run_id, cancelled,
                scope={**scope, "request_id": None, "step_id": step_id or None},
            )
            patch = merge_runtime_updates(values, {"messages": cancelled})
            patch["ledger_pointer"] = {
                **(values.get("ledger_pointer") or {}),
                "applied_seq": seq,
                "applied_offset": self.conversation_files.offset_after(session_id, seq),
            }
            await graph.aupdate_state(config, patch)

    async def mark_terminal_checkpoint(self, session_id: str, run_id: str, status: str, reason: str) -> None:
        graph = await self._get_graph_bounded()
        config = make_thread_config(session_id)
        if not hasattr(graph, "aget_state") or not hasattr(graph, "aupdate_state"):
            return
        snapshot = await graph.aget_state(config)
        values = dict(snapshot.values or {}) if snapshot else {}
        if "context" not in values or (values.get("scope") or {}).get("run_id") not in {None, run_id}:
            return
        await graph.aupdate_state(
            config,
            {
                **terminal_patch(values, status, reason),
                "current_step": {
                    **values.get("current_step", {}),
                    "step_status": status,
                    "tool_calls": [
                        {**call, "status": "cancelled"}
                        if call.get("status") in {"pending", "running"} else call
                        for call in (values.get("current_step", {}).get("tool_calls") or [])
                    ],
                },
                "pending_interaction": None,
            },
        )

    def _message_timestamp(self, msg: BaseMessage) -> int | None:
        ts = msg.additional_kwargs.get("timestamp")
        if isinstance(ts, (int, float)):
            return int(ts)
        return None

    def _compact_tool_content(
        self,
        msg: ToolMessage,
        *,
        session_id: str | None = None,
    ) -> tuple[str, str | None, str | None, bool]:
        content = str(msg.content) if msg.content is not None else ""
        kwargs = getattr(msg, "additional_kwargs", None) or {}
        cache_path = kwargs.get("tool_cache_path")
        cache_summary = kwargs.get("tool_cache_summary")
        if cache_path and cache_summary:
            return str(cache_summary), msg.tool_call_id, str(cache_summary), True
        preview = summarize_tool_result(content, max_chars=800)
        truncated = len(preview) < len(content)
        if truncated and session_id and msg.tool_call_id:
            project = session_project_path(session_id)
            if project is not None:
                try:
                    save_tool_result(session_id, msg.tool_call_id, content, Path(project))
                    return preview, msg.tool_call_id, preview, True
                except Exception:
                    log.warning(
                        "tool result projection cache failed",
                        extra={"session_id": session_id, "tool_call_id": msg.tool_call_id},
                        exc_info=True,
                    )
        return (preview if truncated else content), None, (preview if truncated else None), truncated

    def messages_to_openai(
        self,
        messages: list,
        *,
        session_id: str | None = None,
        compact_tools: bool = False,
        ledger_rows: list[dict] | None = None,
    ) -> list[ChatMessage]:
        rows_by_id = {row["message_id"]: row for row in (ledger_rows or [])}

        def identity(msg: BaseMessage) -> dict:
            ledger_id = (msg.additional_kwargs or {}).get("ledger_message_id")
            row = rows_by_id.get(ledger_id)
            return {
                "message_id": ledger_id,
                "vendor_message_id": msg.id,
                "scope": row["scope"] if row else None,
                "origin": row["origin"] if row else None,
            }
        tool_display_names: dict[str, str] = {}
        for msg in messages:
            if isinstance(msg, ToolMessage) and msg.tool_call_id and msg.name:
                tool_display_names[msg.tool_call_id] = msg.name

        result: list[ChatMessage] = []
        for msg in messages:
            if is_internal_prompt_message(msg):
                continue
            if isinstance(msg, HumanMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                result.append(
                    ChatMessage(role="user", content=content, timestamp=self._message_timestamp(msg), **identity(msg))
                )
            elif isinstance(msg, AIMessage):
                content = msg.content
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                    )
                tool_calls = None
                if msg.tool_calls:
                    tool_calls = []
                    for tc in msg.tool_calls:
                        tc_id = tc.get("id", "")
                        raw_name = tc.get("name", "")
                        raw_args = tc.get("args") or {}
                        if isinstance(raw_args, str):
                            try:
                                raw_args = json.loads(raw_args)
                            except json.JSONDecodeError:
                                raw_args = {}
                        display_name, display_args = resolve_tool_display(raw_name, raw_args)
                        if tc_id and tc_id in tool_display_names:
                            display_name = tool_display_names[tc_id]
                        if not should_show_tool_in_ui(display_name):
                            continue
                        tool_calls.append(
                            ToolCall(
                                id=tc_id,
                                function=ToolCallFunction(
                                    name=display_name,
                                    arguments=json.dumps(display_args, ensure_ascii=False),
                                ),
                            )
                        )
                    if not tool_calls:
                        tool_calls = None
                if not content and not tool_calls:
                    continue
                result.append(
                    ChatMessage(
                        role="assistant",
                        content=content or None,
                        tool_calls=tool_calls,
                        timestamp=self._message_timestamp(msg),
                        **identity(msg),
                    )
                )
            elif isinstance(msg, ToolMessage):
                display_name = msg.name or "tool"
                if not should_show_tool_in_ui(display_name):
                    continue
                kwargs = msg.additional_kwargs or {}
                tool_status = str(kwargs.get("tool_status") or (
                    "failed" if kwargs.get("reason_code") else "completed"
                ))
                result.append(
                    ChatMessage(
                        role="tool",
                        # History shows the execution result only. Full tool
                        # output remains in the append-only ledger/cache.
                        content=None,
                        tool_call_id=msg.tool_call_id,
                        timestamp=self._message_timestamp(msg),
                        tool_status=tool_status,
                        tool_reason_code=str(kwargs["reason_code"]) if kwargs.get("reason_code") else None,
                        **identity(msg),
                    )
                )
        return result

    async def get_history(self, session_id: str) -> SessionHistory:
        return await self._history_from_checkpoint(session_id, compact_tools=False)
