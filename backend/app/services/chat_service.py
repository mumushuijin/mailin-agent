import asyncio
import hashlib
import json
import logging
import uuid
import threading
from collections.abc import AsyncIterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agent.messages import is_ephemeral_prompt_frame, is_internal_prompt_message
from app.agent.graph import get_graph, make_initial_state, make_thread_config
from app.agent.state import REQUIRED_TOP_LEVEL, to_runtime_view, merge_runtime_updates, to_graph_state, terminal_patch, utc_now
from app.agent.hooks import (
    ON_SESSION_END,
    ON_SESSION_START,
    dispatch_observe,
    dispatch_post_llm_call,
)
from app.agent.streaming.events import AgentEvent, to_sse
from app.agent.streaming.sse_mapper import extract_final_content, stream_graph_events
from app.context.engine import resolve_context_usage
from app.context.memory.maintenance import prepare_session_memory
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
    ToolResultPayload,
)
from app.core.exceptions import AppError
from app.storage.history_projection import HistoryProjectionStore, project_ledger
from app.storage.ledger_contract import PROJECTION_VERSION
from app.storage.conversation_files import SessionConversationFiles
from app.storage.project import require_bound_session, session_project_path
from app.storage.workspace import SessionStore
from app.tools.registry import load_full_config
from app.tools.tool_search import resolve_tool_display, should_show_tool_in_ui

log = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = 30
MAX_HISTORY_LIMIT = 200
PREPARE_MEMORY_TIMEOUT_SECONDS = 8.0

_session_execution_lock = threading.Lock()
_session_executions: set[tuple[str, str]] = set()


@contextmanager
def _session_execution(workspace: Path, session_id: str):
    key = (str(workspace.resolve()), session_id)
    with _session_execution_lock:
        if key in _session_executions:
            raise AppError("该会话已有进行中的 turn，请等待或取消后重试", status_code=409)
        _session_executions.add(key)
    try:
        yield
    finally:
        with _session_execution_lock:
            _session_executions.discard(key)


def _exclusive_turn(*, stream: bool = False):
    def decorate(method):
        if stream:
            @wraps(method)
            async def events(self, message, session_id, *, turn_id=None, submission_id=None,
                             branch_id="main", fork_cursor=None):
                try:
                    sid = self._require_bound_session(session_id)
                    with _session_execution(self.settings.workspace_path, sid):
                        async for event in method(self, message, sid, turn_id=turn_id, submission_id=submission_id,
                                                  branch_id=branch_id, fork_cursor=fork_cursor):
                            event.branch_id = event.branch_id or branch_id
                            yield event
                except AppError as exc:
                    yield AgentEvent("error", {"error": exc.message, "reason_code": exc.code or ("session_busy" if exc.status_code == 409 else "invalid_session")}, turn_id, branch_id=branch_id)
                except ValueError as exc:
                    yield AgentEvent("error", {"error": str(exc), "reason_code": "unsupported_ledger"}, turn_id)
            return events
        @wraps(method)
        async def invoke(self, message, session_id, *, turn_id=None, submission_id=None,
                         branch_id="main", fork_cursor=None):
            sid = self._require_bound_session(session_id)
            with _session_execution(self.settings.workspace_path, sid):
                return await method(self, message, sid, turn_id=turn_id, submission_id=submission_id,
                                    branch_id=branch_id, fork_cursor=fork_cursor)
        return invoke
    return decorate


class ChatService:
    def __init__(self):
        self.settings = get_settings()
        self.session_store = SessionStore(self.settings.workspace_path)
        self.history_projection = HistoryProjectionStore(self.settings.workspace_path)
        self.conversation_files = SessionConversationFiles(self.settings.workspace_path)

    def _new_run_state(
        self, session_id: str, message: str, max_steps: int, turn_id: str | None,
        previous: dict[str, Any] | None = None,
        *, submission_id: str | None = None, branch_id: str = "main",
    ):
        if previous and previous.get("turn_status") in {"running", "waiting_user"}:
            raise AppError("上一 turn 尚未结束，请先恢复或取消", status_code=409)
        state = make_initial_state(
            message, max_steps, session_id=session_id,
            workspace_id=str(self.settings.workspace_path), turn_id=turn_id,
        )
        self.conversation_files.ensure_metadata(session_id)
        accepted_message_id = (
            "msg_submission_" + hashlib.sha256(submission_id.encode("utf-8")).hexdigest()[:32]
            if submission_id else f"msg_user_{state['scope']['turn_id']}"
        )
        if self.conversation_files.message_by_id(session_id, accepted_message_id):
            raise AppError("该 turn 已提交，不能重复执行；请加载会话历史查看结果", status_code=409)
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
            previous_todos = (previous.get("task_details") or {}).get("_runtime", {}).get("todos")
            if previous_todos:
                state["task_details"]["_runtime"] = {"todos": list(previous_todos)}
        state["context"]["bootstrap_fingerprint"] = self._bootstrap_fingerprint(session_id)
        user_message = state["context"]["working_message"][-1]
        user_message.additional_kwargs["ledger_message_id"] = accepted_message_id
        seq = self.conversation_files.append_messages(
            session_id,
            str(state["scope"]["turn_id"]),
            [state["context"]["working_message"][-1]],
            scope=state["scope"],
            submission_id=submission_id, branch_id=branch_id,
        )
        state["ledger_pointer"] = self.conversation_files.ledger_pointer(session_id, seq, branch_id=branch_id)
        return state

    def _existing_submission(self, session_id: str, submission_id: str | None, message: str,
                             *, branch_id: str | None = None):
        if not submission_id:
            return None
        self.conversation_files.ensure_metadata(session_id)
        event = self.conversation_files.find_submission(session_id, submission_id)
        if not event:
            return None
        stored = self.conversation_files.resolve_message(session_id, event["payload"].get("message") or {})
        if stored.get("content") != message:
            raise AppError("submission_id 已绑定不同内容", status_code=409, code="submission_conflict")
        if branch_id is not None and event["branch_id"] != branch_id:
            raise AppError("submission_id 已绑定不同分支", status_code=409, code="submission_conflict")
        branch_id = event["branch_id"]
        events = self.conversation_files.read_events(session_id)
        from app.storage.event_projection import project_events
        projection = project_events(events, branch_id=branch_id, session_id=session_id)
        turn_id = event.get("turn_id") or ""
        turn = projection["turns"].get(turn_id) or {}
        final_message_id = turn.get("final_message_id")
        content = ""
        if final_message_id:
            row = self.conversation_files.message_by_id(session_id, final_message_id)
            if row is not None:
                value = self.conversation_files.resolve_message(session_id, row["message"]).get("content")
            else:
                final = next((projection["messages"][key] for key in projection["message_order"]
                              if projection["messages"][key].get("message_id") == final_message_id), None)
                value = (final or {}).get("content")
            content = value if isinstance(value, str) else "".join(
                str(block.get("text") or "") for block in (value or [])
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return {"event": event, "turn_id": turn_id, "status": turn.get("status", "running"),
                "content": content, "cursor": projection.get("cursor")}

    def _durable_event_frames(self, session_id: str, after_seq: int, *, state_revision: int | None = None,
                              turn_id: str | None = None, branch_id: str | None = None) -> list[AgentEvent]:
        frames = []
        from app.services.timeline_service import normalize_timeline_event

        for event in self.conversation_files.read_events(session_id, after_seq=after_seq, branch_id=branch_id):
            if turn_id is not None and event.get("turn_id") != turn_id:
                continue
            frames.append(AgentEvent(
                "timeline_event",
                {"session_id": session_id, "event": normalize_timeline_event(event, self.conversation_files), "cursor": {
                    "branch_id": event["branch_id"],
                    "event_seq": event["event_seq"],
                    "event_id": event["event_id"],
                }},
                event.get("turn_id"), event_id=event["event_id"],
                event_seq=event["event_seq"], branch_id=event["branch_id"],
                state_revision=state_revision,
            ))
        return frames

    def _branch_history_state(self, session_id: str, branch_id: str, *,
                              as_of_event_seq: int | None = None) -> dict[str, Any]:
        """Build a new-turn window from recorded facts without resuming old work."""
        from app.agent.recovery import decode_window_row, is_audit_row
        from app.storage.event_projection import project_events

        events = self.conversation_files.read_events(session_id)
        known = {"main"} | {event["branch_id"] for event in events if event["event_type"] == "branch.forked"}
        if branch_id not in known:
            raise AppError("未知的历史分支", status_code=409, code="invalid_branch")
        projection = project_events(events, branch_id=branch_id, session_id=session_id,
                                    as_of_event_seq=as_of_event_seq)
        visible_ids = set(projection["applied_events"])
        rows = [self.conversation_files._event_to_projection(event) for event in events if event["event_id"] in visible_ids]
        rows = [row for row in rows if row is not None and not is_audit_row(row)]
        unresolved_calls: set[str] = set()
        for row in rows:
            unresolved_calls.update(call["id"] for call in row["message"].get("tool_calls") or [])
            if row["message"].get("role") == "tool":
                unresolved_calls.discard(row["message"].get("tool_call_id"))
        if unresolved_calls:
            raise AppError("历史位置包含未结算的工具调用，请选择工具结果之后的位置继续", status_code=409,
                           code="historical_tool_state_incomplete")
        previous = make_initial_state("", self._max_steps(), session_id=session_id,
                                      workspace_id=str(self.settings.workspace_path))
        previous["context"]["working_message"] = [decode_window_row(self.conversation_files, session_id, row) for row in rows]
        previous["context"]["bootstrap_fingerprint"] = self._bootstrap_fingerprint(session_id)
        previous["turn_status"] = "completed"
        cursor = projection.get("cursor")
        previous["ledger_pointer"] = self.conversation_files.ledger_pointer(
            session_id, cursor["event_seq"] if cursor else 0, branch_id=branch_id,
        )
        return previous

    async def _previous_for_branch(self, graph, config: dict, session_id: str, branch_id: str,
                                   fork_cursor: dict[str, Any] | None) -> dict[str, Any]:
        self.conversation_files.ensure_metadata(session_id)
        if fork_cursor is not None:
            # Validate the historical window before creating a branch, so an
            # unusable tool prefix leaves no misleading child execution.
            self._branch_history_state(session_id, fork_cursor["branch_id"],
                                       as_of_event_seq=fork_cursor["event_seq"])
            self.conversation_files.fork_branch(session_id, branch_id, parent_cursor=fork_cursor)
            return self._branch_history_state(session_id, branch_id)
        await self._reject_legacy_checkpoint(graph, config)
        values = await self._checkpoint_values(graph, config)
        if not values and branch_id != "main":
            return self._branch_history_state(session_id, branch_id)
        return self._refresh_stale_context(
            session_id, self._recover_ledger_tail(session_id, values, stop_incomplete=True, branch_id=branch_id)
        )

    def _bootstrap_fingerprint(self, session_id: str) -> str:
        from app.context.bootstrap import load_bootstrap

        project = session_project_path(session_id)
        text, _tokens = load_bootstrap(self.settings.workspace_path, Path(project) if project else None)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _recover_ledger_tail(self, session_id: str, previous: dict[str, Any], *, stop_incomplete: bool = False,
                              branch_id: str | None = None) -> dict[str, Any]:
        from app.agent.recovery import recover_ledger_tail
        return recover_ledger_tail(previous, self.conversation_files, session_id=session_id,
                                   stop_incomplete=stop_incomplete, branch_id=branch_id)

    def _refresh_stale_context(self, session_id: str, previous: dict[str, Any]) -> dict[str, Any]:
        if not previous:
            return previous
        context = dict(previous.get("context") or {})
        fingerprint = self._bootstrap_fingerprint(session_id)
        if context.get("bootstrap_fingerprint") == fingerprint:
            return previous
        # Bootstrap/reference changes invalidate the prepared window. Rebuild it
        # from the durable ledger once, then let the existing compression pipeline
        # establish a fresh token-bounded working window before the next model call.
        branch_id = ((previous.get("ledger_pointer") or {}).get("event_cursor") or {}).get("branch_id", "main")
        rows = self.conversation_files.read(session_id, branch_id=branch_id)
        from app.agent.recovery import decode_window_row, is_audit_row
        rebuilt = dict(previous)
        context["working_message"] = [
            decode_window_row(self.conversation_files, session_id, row)
            for row in rows if not is_audit_row(row)
        ]
        context["bootstrap_fingerprint"] = fingerprint
        context["window_pointer"] = None
        context["last_built_at"] = None
        rebuilt["context"] = context
        if rows:
            pointer = dict(previous.get("ledger_pointer") or {})
            pointer["applied_seq"] = int(rows[-1]["seq"])
            pointer["applied_offset"] = self.conversation_files.offset_after(session_id, int(rows[-1]["seq"]))
            rebuilt["ledger_pointer"] = self.conversation_files.ledger_pointer(session_id, int(rows[-1]["seq"]), branch_id=branch_id)
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
        from app.agent.state import validate_agent_state
        try:
            validate_agent_state(values)
        except ValueError as exc:
            raise RuntimeError(f"不支持旧版或无效 Agent checkpoint；请处理旧会话后新建：{exc}") from exc

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
        # The legacy history API and its single session cache represent main.
        # Branch timelines use the explicit branch-aware timeline API.
        from app.storage.ledger_contract import is_audit_row
        ledger_rows = self.conversation_files.read(session_id, branch_id="main")
        messages = [self.conversation_files.decode_row(row, session_id=session_id, resolve_references=True)
                    for row in ledger_rows if not is_audit_row(row)]
        projection = project_ledger(ledger_rows)
        session_token_stats = projected.get("session_token_stats") or {}
        api_usage = projection.get("api_usage") or projected.get("api_usage") or {}
        context_usage = resolve_context_usage({**projected, "messages": messages}, session_id)
        return SessionHistory(
            projection_version=PROJECTION_VERSION,
            session_id=session_id,
            workspace_id=self.conversation_files.workspace_id,
            messages=[ChatMessage(**row) for row in projection["messages"]],
            timelines=projection["timelines"],
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
        await self._reject_legacy_checkpoint(graph, config)
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
            config = make_thread_config(session_id, "main")
            state = await graph.aget_state(config)
            values = dict(state.values) if state and state.values else {}
            await self._reject_legacy_checkpoint(graph, config)
            history = self._history_from_values(session_id, values, compact_tools=True)
            self.history_projection.write(
                session_id,
                history.messages,
                context_usage=history.context_usage,
                api_usage=history.api_usage,
                session_token_stats=history.session_token_stats,
                todos=history.todos,
                timelines=history.timelines,
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
        latency = RunLatency(turn_id=None, session_id=session_id)
        latency.start(LATENCY_STAGE_HISTORY_LOAD)
        meta = self.session_store.get(session_id)
        limit = max(1, min(MAX_HISTORY_LIMIT, int(limit or DEFAULT_HISTORY_LIMIT)))

        payload = self.history_projection.read(session_id)
        if (payload is None or payload.get("branch_id") != "main"
                or int(payload.get("ledger_seq", -1)) < self.conversation_files.latest_seq(session_id)):
            ledger_rows = self.conversation_files.read(session_id, branch_id="main")
            if ledger_rows:
                projection = project_ledger(ledger_rows)
                history = SessionHistory(
                    projection_version=PROJECTION_VERSION,
                    session_id=session_id,
                    workspace_id=self.conversation_files.workspace_id,
                    messages=[ChatMessage(**row) for row in projection["messages"]],
                    timelines=projection["timelines"],
                    api_usage=projection.get("api_usage"),
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
                timelines=history.timelines,
                ledger_seq=self.conversation_files.latest_seq(session_id),
            )
            payload = self.history_projection.read(session_id) or {
                "messages": [m.model_dump(mode="json", exclude_none=True, exclude_defaults=True) for m in history.messages],
                "timelines": history.timelines,
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
            projection_version=PROJECTION_VERSION,
            session_id=session_id,
            workspace_id=self.conversation_files.workspace_id,
            session=Session(**meta),
            messages=page_messages,
            context_usage=payload.get("context_usage"),
            api_usage=payload.get("api_usage"),
            session_token_stats=payload.get("session_token_stats"),
            todos=payload.get("todos"),
            timelines=payload.get("timelines", []),
            limit=limit,
            before=before,
            has_more=has_more,
            next_cursor=next_cursor,
            projection_updated_at=payload.get("updated_at"),
        )

    async def get_tool_result(self, session_id: str, tool_call_id: str, *, seq: int | None = None) -> ToolResultPayload:
        self.session_store.get(session_id)
        try:
            if seq is not None:
                row = self.conversation_files.row_at(session_id, seq)
                if row is not None and (row["message"].get("role") != "tool"
                                        or row["message"].get("tool_call_id") != tool_call_id):
                    raise ValueError("工具结果序号与调用身份不匹配")
            else:
                rows = self.conversation_files.read(session_id)
                row = next((row for row in reversed(rows) if row["message"].get("role") == "tool"
                            and row["message"].get("tool_call_id") == tool_call_id), None)
            if row is None:
                raise ValueError("工具结果不可用或尚未落盘")
            message = self.conversation_files.decode_row(row, session_id=session_id, resolve_references=True)
            from app.storage.message_codec import safe_business_value
            content = safe_business_value(message.content)
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            return ToolResultPayload(session_id=session_id, tool_call_id=tool_call_id, content=content)
        except Exception as exc:
            return ToolResultPayload(
                session_id=session_id,
                tool_call_id=tool_call_id,
                content=None,
                available=False,
                error=str(exc),
            )

    async def get_ledger_message(self, session_id: str, seq: int) -> dict:
        self.session_store.get(session_id)
        row = self.conversation_files.row_at(session_id, seq)
        if row is None:
            raise AppError("账本消息不存在", status_code=404)
        from app.storage.message_codec import safe_business_value
        safe = safe_business_value({key: value for key, value in row.items() if not key.startswith("_")})
        # Only the business message and public outcome are a UI resource.
        safe.pop("extensions", None)
        safe.pop("model", None)
        safe["message"].pop("id", None)
        return {"session_id": session_id, "workspace_id": self.conversation_files.workspace_id, "message": safe}

    async def _llm_short_title(self, user_message: str, assistant_content: str, *, session_id: str, turn_id: str,
                               branch_id: str = "main") -> str | None:
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
            from app.agent.audit import RuntimeLedgerAudit, bind_runtime_audit
            from app.core.model_request import bind_model_request_scope, invoke_auxiliary_model_async
            scope = {"workspace_id": self.conversation_files.workspace_id, "session_id": session_id, "turn_id": turn_id}
            with bind_model_request_scope(scope), bind_runtime_audit(RuntimeLedgerAudit(self.conversation_files, scope, branch_id=branch_id)):
                result = await asyncio.wait_for(
                    invoke_auxiliary_model_async(model, [HumanMessage(content=prompt)]), timeout=8,
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
        turn_id: str,
        branch_id: str = "main",
    ) -> None:
        try:
            meta = self.session_store.get(session_id)
            if meta.get("title_source") != "auto":
                return
            if not (assistant_content or "").strip():
                return
            summary = await self._llm_short_title(user_message, assistant_content, session_id=session_id, turn_id=turn_id,
                                                  branch_id=branch_id)
            if summary:
                self.session_store.apply_auto_title(session_id, summary)
        except Exception:
            return

    @_exclusive_turn()
    async def send_sync(
        self,
        message: str,
        session_id: str | None,
        *,
        turn_id: str | None = None,
        submission_id: str | None = None,
        branch_id: str = "main",
        fork_cursor: dict[str, Any] | None = None,
    ) -> ChatResponse:
        from app.core.settings import get_settings

        turn_id = turn_id or "turn_" + uuid.uuid4().hex
        latency = RunLatency(turn_id=turn_id, session_id=session_id)
        sid = self._require_bound_session(session_id)
        latency.bind_session(sid)

        existing = self._existing_submission(sid, submission_id, message, branch_id=branch_id)
        if existing:
            return ChatResponse(content=existing["content"], session_id=sid, turn_id=existing["turn_id"], branch_id=branch_id)

        if not get_settings().has_llm:
            raise RuntimeError("未配置 LLM API Key")
        self.session_store.maybe_set_title_from_message(sid, message)
        latency.start(LATENCY_STAGE_CONTEXT_PREPARE)
        await self._prepare_memory_bounded(message, latency)
        graph = await self._get_graph_bounded()
        config = make_thread_config(sid, branch_id)
        previous = await self._previous_for_branch(graph, config, sid, branch_id, fork_cursor)
        max_steps = self._max_steps()
        state = self._new_run_state(sid, message, max_steps, turn_id, previous, submission_id=submission_id, branch_id=branch_id)
        turn_id = str(state["scope"]["turn_id"])
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
            await self.repair_cancelled_checkpoint(sid, branch_id=branch_id)
            await self.mark_terminal_checkpoint(sid, turn_id, "failed", "request_timeout", branch_id=branch_id)
            raise RuntimeError(
                f"请求超时（{int(GRAPH_REQUEST_TIMEOUT_SECONDS)}s），请稍后重试"
            ) from exc
        except Exception:
            await self.repair_cancelled_checkpoint(sid, branch_id=branch_id)
            await self.mark_terminal_checkpoint(sid, turn_id, "failed", "runtime_error", branch_id=branch_id)
            raise
        projected_result = to_runtime_view(result)
        from app.agent.streaming.interrupts import has_pending_interrupt
        if await has_pending_interrupt(graph, config):
            raise RuntimeError("此操作需要通过 WebSocket 确认或回答，请重连当前会话")
        content = extract_final_content(projected_result.get("messages", []))
        self.session_store.touch(sid)
        await self._refresh_history_projection(sid, graph=graph, config=config)
        self._schedule_background(
            "auto-title refinement",
            lambda: self._refine_auto_title_after_turn(sid, message, content, turn_id, branch_id),
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
        return ChatResponse(content=content, session_id=sid, turn_id=turn_id, branch_id=branch_id)

    @_exclusive_turn(stream=True)
    async def iter_chat_events(
        self,
        message: str,
        session_id: str | None,
        *,
        turn_id: str | None = None,
        submission_id: str | None = None,
        branch_id: str = "main",
        fork_cursor: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """聊天主流程：产出统一 AgentEvent 流（SSE / WS 共用）。"""
        turn_id = turn_id or "turn_" + uuid.uuid4().hex
        latency = RunLatency(turn_id=turn_id, session_id=session_id)
        try:
            sid = self._require_bound_session(session_id)
        except AppError as exc:
            yield AgentEvent("error", {"error": exc.message}, turn_id)
            return
        latency.bind_session(sid)

        try:
            existing = self._existing_submission(sid, submission_id, message, branch_id=branch_id)
        except AppError as exc:
            yield AgentEvent("error", {"error": exc.message, "reason_code": "submission_conflict"}, turn_id)
            return
        if existing:
            event = existing["event"]
            original_turn = existing["turn_id"]
            delivery_turn = turn_id
            yield AgentEvent("session", {"session_id": sid, "turn_id": original_turn, "branch_id": branch_id}, delivery_turn,
                             branch_id=branch_id)
            for frame in self._durable_event_frames(sid, event["event_seq"] - 1, turn_id=original_turn, branch_id=branch_id):
                yield frame.with_turn_id(delivery_turn)
            settled = existing["status"] in {"completed", "failed", "cancelled", "handoff"}
            yield AgentEvent(
                "done",
                {"content": existing["content"], "session_id": sid, "partial": not settled,
                 "turn_id": original_turn, "duplicate_submission": True,
                 "cursor": existing["cursor"], "branch_id": branch_id},
                delivery_turn,
                branch_id=branch_id,
            )
            return

        if not self.settings.has_llm:
            yield AgentEvent("error", {"error": "未配置 LLM API Key，请在 backend/.env 设置 OPENAI_API_KEY"}, turn_id)
            return
        yield AgentEvent("session", {"session_id": sid, "branch_id": branch_id}, turn_id, branch_id=branch_id)
        yield AgentEvent("stage", latency.instant(LATENCY_STAGE_ACCEPTED), turn_id)
        self.session_store.maybe_set_title_from_message(sid, message)

        yield AgentEvent("stage", latency.start(LATENCY_STAGE_CONTEXT_PREPARE), turn_id)
        memory_status = await self._prepare_memory_bounded(message, latency)
        try:
            graph = await self._get_graph_bounded()
        except Exception as exc:
            yield AgentEvent(
                "stage",
                latency.finish(LATENCY_STAGE_CONTEXT_PREPARE, "failed", error=str(exc)),
                turn_id,
            )
            yield AgentEvent("error", {"error": str(exc)}, turn_id)
            log.info("chat.run.completed", extra=latency.summary(status="failed", failed_stage=LATENCY_STAGE_CONTEXT_PREPARE))
            return
        config = make_thread_config(sid, branch_id)
        try:
            previous = await self._previous_for_branch(graph, config, sid, branch_id, fork_cursor)
        except AppError as exc:
            yield AgentEvent("error", {"error": exc.message, "reason_code": exc.code or "invalid_branch_cursor"}, turn_id)
            return
        except (RuntimeError, ValueError) as exc:
            yield AgentEvent("error", {"error": str(exc), "reason_code": "unsupported_checkpoint"}, turn_id)
            return
        max_steps = self._max_steps()
        state = self._new_run_state(sid, message, max_steps, turn_id, previous, submission_id=submission_id, branch_id=branch_id)
        turn_id = str(state["scope"]["turn_id"])
        durable_seq = int(state["ledger_pointer"]["applied_seq"]) - 1
        if fork_cursor is not None:
            fork_event = next(event for event in self.conversation_files.read_events(sid, branch_id=branch_id)
                              if event["event_type"] == "branch.forked")
            durable_seq = min(durable_seq, fork_event["event_seq"] - 1)
        state_revision = int(state.get("state_revision", 0))
        for frame in self._durable_event_frames(sid, durable_seq, state_revision=state_revision, branch_id=branch_id):
            durable_seq = max(durable_seq, frame.event_seq or durable_seq)
            yield frame
        yield AgentEvent(
            "stage",
            latency.finish(LATENCY_STAGE_CONTEXT_PREPARE, memory_status),
            turn_id,
        )
        yield AgentEvent("stage", latency.start(LATENCY_STAGE_MODEL_STREAM), turn_id)

        nudge_pending = False
        final_content = ""
        completed = False
        async for event in stream_graph_events(graph, state, config, max_steps, turn_id=turn_id):
            current_revision = int(event.state.get("state_revision", state_revision)) if event.state else state_revision
            for frame in self._durable_event_frames(sid, durable_seq, state_revision=current_revision, branch_id=branch_id):
                durable_seq = max(durable_seq, frame.event_seq or durable_seq)
                yield frame
            if event.type == "done":
                event.branch_id = branch_id
                event.data["branch_id"] = branch_id
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
                    turn_id,
                )
            elif event.type == "error":
                yield AgentEvent(
                    "stage",
                    latency.finish(LATENCY_STAGE_MODEL_STREAM, "failed"),
                    turn_id,
                )
            yield event

        for frame in self._durable_event_frames(sid, durable_seq, state_revision=state_revision, branch_id=branch_id):
            yield frame

        if completed:
            self._schedule_background(
                "auto-title refinement",
                lambda: self._refine_auto_title_after_turn(sid, message, final_content, turn_id, branch_id),
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
                turn_id,
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
        turn_id: str | None = None,
        submission_id: str | None = None,
        branch_id: str = "main",
        fork_cursor: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        from app.agent.streaming.interrupts import has_pending_interrupt

        sid: str | None = session_id
        transport_id = "sse_" + uuid.uuid4().hex
        transport_seq = 0
        async for event in self.iter_chat_events(message, session_id, turn_id=turn_id, submission_id=submission_id,
                                                branch_id=branch_id, fork_cursor=fork_cursor):
            if event.type == "session" and event.data.get("session_id"):
                sid = event.data["session_id"]
            transport_seq += 1
            yield to_sse(event, transport_id=transport_id, transport_seq=transport_seq)

        if not sid:
            return
        graph = await self._get_graph_bounded()
        if await has_pending_interrupt(graph, make_thread_config(sid, branch_id)):
            transport_seq += 1
            yield to_sse(
                AgentEvent(
                    "error",
                    {
                        "error": "此操作需要在 WebSocket 连接下确认或回答，请重连后重试",
                        "fail_closed": True,
                        "reason_code": "sse_no_answer_channel",
                    },
                    turn_id,
                ),
                transport_id=transport_id,
                transport_seq=transport_seq,
            )

    async def resume_chat_events(
        self,
        session_id: str,
        decision: Any,
        *,
        turn_id: str | None = None,
        branch_id: str = "main",
    ) -> AsyncIterator[AgentEvent]:
        """恢复 interrupt 后继续产出事件流。"""
        from langgraph.types import Command

        graph = await self._get_graph_bounded()
        config = make_thread_config(session_id, branch_id)
        await self._reject_legacy_checkpoint(graph, config)
        values = self._recover_ledger_tail(session_id, await self._checkpoint_values(graph, config), branch_id=branch_id)
        durable_seq = int((values.get("ledger_pointer") or {}).get("applied_seq") or 0)
        state_revision = int(values.get("state_revision", 0))
        if values.get("turn_status") in {"completed", "failed", "cancelled", "handoff"}:
            raise RuntimeError("该 turn 已结束，不能恢复过期交互")
        checkpoint_turn_id = (values.get("scope") or {}).get("turn_id")
        if turn_id and checkpoint_turn_id and turn_id != checkpoint_turn_id:
            raise RuntimeError("恢复请求与 checkpoint 中的 turn 不匹配")
        turn_id = str(checkpoint_turn_id or turn_id or "")
        pending = values.get("pending_interaction") if values else None
        if not isinstance(pending, dict) or not pending.get("id"):
            raise RuntimeError("没有可恢复的有效交互")
        if isinstance(pending, dict):
            if isinstance(decision, dict):
                decision_kind = "approval" if decision.get("kind") == "tool_approval" else decision.get("kind")
                if decision_kind and decision_kind != pending["kind"]:
                    raise RuntimeError("响应与 checkpoint 中的交互类型不匹配")
                if decision.get("step_id") != values.get("current_step", {}).get("step_id"):
                    raise RuntimeError("响应与 checkpoint 中的 step 不匹配")
                if decision.get("mode") and decision["mode"] != pending.get("mode"):
                    raise RuntimeError("响应与 checkpoint 中的交互模式不匹配")
                if decision.get("tool_call_id") != pending.get("call_id"):
                    raise RuntimeError("响应与 checkpoint 中的调用不匹配")
            response_id = decision.get("interrupt_id") if isinstance(decision, dict) else None
            if isinstance(decision, dict) and response_id != pending.get("id"):
                raise RuntimeError("响应与 checkpoint 中的 interrupt 不匹配")
            if pending.get("decision") is not None:
                decision = pending["decision"]
            else:
                interaction = {"id": pending["id"], "kind": pending["kind"], "decision": decision}
                message_payload = {"role": "user", "content": (
                    str(decision.get("answer") or "已确认交还") if isinstance(decision, dict) else str(decision)
                )}
                if isinstance(decision, dict) and decision.get("kind") == "tool_approval":
                    message_payload["content"] = decision["decision"]
                if isinstance(decision, dict) and decision.get("source") in {"timeout", "cancelled"}:
                    message_payload = {"role": "system", "content": [{"type": "lifecycle", "subject": "interaction"}]}
                if pending.get("call_id"):
                    message_payload["tool_call_id"] = pending["call_id"]
                source = decision.get("source") if isinstance(decision, dict) else None
                answer = decision.get("decision") if isinstance(decision, dict) else decision
                interaction_status = "expired" if source == "timeout" else "cancelled" if source == "cancelled" else "denied" if answer == "deny" else "completed"
                seq, _ = self.conversation_files.append(
                    session_id, f"msg_decision_{pending['id']}", message_payload,
                    turn_id=turn_id, interaction=interaction,
                    execution={"status": interaction_status, "finished_at": utc_now()},
                    branch_id=branch_id,
                )
                values["ledger_pointer"] = self.conversation_files.ledger_pointer(session_id, seq, branch_id=branch_id)
            # Commit the durable decision together with the native resume. A
            # separate update here would clear LangGraph's pending interrupt.
            values["state_revision"] = int(values.get("state_revision", 0)) + 1
            values["checkpoint_uid"] = f"cp_{uuid.uuid4().hex}"
            values.pop("checkpoint_id", None)
            values["pending_interaction"] = {**pending, "decision": decision}
        max_steps = self._max_steps()
        if isinstance(decision, dict) and decision.get("kind") == "tool_approval":
            decision = decision["decision"]
        latency = RunLatency(turn_id=turn_id, session_id=session_id)
        for frame in self._durable_event_frames(session_id, durable_seq, state_revision=state_revision, branch_id=branch_id):
            durable_seq = max(durable_seq, frame.event_seq or durable_seq)
            yield frame
        yield AgentEvent("stage", latency.start(LATENCY_STAGE_MODEL_STREAM), turn_id, branch_id=branch_id)

        async for event in stream_graph_events(
            graph,
            {},
            config,
            max_steps,
            turn_id=turn_id,
            input_override=Command(resume=decision, update=to_graph_state(values)),
        ):
            current_revision = int(event.state.get("state_revision", state_revision)) if event.state else state_revision
            for frame in self._durable_event_frames(session_id, durable_seq, state_revision=current_revision, branch_id=branch_id):
                durable_seq = max(durable_seq, frame.event_seq or durable_seq)
                yield frame
            event.branch_id = branch_id
            event.data["branch_id"] = branch_id
            if event.type == "done":
                self.session_store.touch(session_id)
                yield AgentEvent("stage", latency.finish(LATENCY_STAGE_MODEL_STREAM), turn_id, branch_id=branch_id)
                self._schedule_background(
                    "history projection refresh",
                    lambda: self._refresh_history_projection(session_id, graph=graph, config=config),
                )
            elif event.type == "error":
                yield AgentEvent("stage", latency.finish(LATENCY_STAGE_MODEL_STREAM, "failed"), turn_id, branch_id=branch_id)
            yield event
        for frame in self._durable_event_frames(session_id, durable_seq, state_revision=state_revision, branch_id=branch_id):
            yield frame

    async def repair_cancelled_checkpoint(self, session_id: str, *, branch_id: str = "main") -> None:
        """取消时修复未完成的 tool_calls。"""
        graph = await self._get_graph_bounded()
        config = make_thread_config(session_id, branch_id)
        snapshot = await graph.aget_state(config)
        if not snapshot or not snapshot.values:
            return
        values = dict(snapshot.values or {})
        recovered = self._recover_ledger_tail(session_id, values, branch_id=branch_id)
        rows = self.conversation_files.read(session_id, branch_id=branch_id)
        settled = {row["message"].get("tool_call_id") for row in rows
                   if row.get("turn_id") == values["scope"]["turn_id"] and row["message"].get("role") == "tool"}
        pending_messages = []
        for msg in reversed(recovered.get("context", {}).get("working_message", [])):
            if isinstance(msg, AIMessage) and msg.tool_calls:
                for call in msg.tool_calls:
                    if call["id"] not in settled:
                        pending_messages.append(ToolMessage(
                            content="工具调用在执行前取消。", tool_call_id=call["id"],
                            additional_kwargs={"tool_status": "cancelled", "reason_code": "cancelled",
                                               "ledger_message_id": f"msg_cancelled_{values['scope']['turn_id']}_{call['id']}"},
                        ))
                break
        if pending_messages:
            from app.agent.audit import RuntimeLedgerAudit
            audit = RuntimeLedgerAudit(self.conversation_files, values["scope"], branch_id=branch_id)
            pending_messages = [audit.settle_tool(message) for message in pending_messages]
            seq = max(int(recovered["ledger_pointer"]["applied_seq"]),
                      *(int(message.additional_kwargs["ledger_seq"]) for message in pending_messages))
            recovered = merge_runtime_updates(recovered, {"messages": pending_messages})
            recovered["ledger_pointer"] = self.conversation_files.ledger_pointer(session_id, seq, branch_id=branch_id)
        await graph.aupdate_state(config, to_graph_state(recovered))

    async def mark_terminal_checkpoint(self, session_id: str, turn_id: str, status: str, reason: str, *,
                                        branch_id: str = "main") -> None:
        graph = await self._get_graph_bounded()
        config = make_thread_config(session_id, branch_id)
        if not hasattr(graph, "aget_state") or not hasattr(graph, "aupdate_state"):
            return
        snapshot = await graph.aget_state(config)
        values = dict(snapshot.values or {}) if snapshot else {}
        if "context" not in values or (values.get("scope") or {}).get("turn_id") not in {None, turn_id}:
            return
        values = self._recover_ledger_tail(session_id, values, branch_id=branch_id)
        if values.get("turn_status") in {"completed", "failed", "cancelled", "handoff"}:
            await graph.aupdate_state(config, to_graph_state(values))
            return
        from app.agent.audit import RuntimeLedgerAudit
        seq = RuntimeLedgerAudit(self.conversation_files, values["scope"], branch_id=branch_id).turn_terminal(status, reason)
        await graph.aupdate_state(
            config,
            {
                **terminal_patch(values, status, reason),
                "ledger_pointer": self.conversation_files.ledger_pointer(session_id, seq, branch_id=branch_id),
                "current_step": {
                    **values.get("current_step", {}),
                    "step_status": "skipped" if status == "handoff" else status,
                    "tool_calls": values.get("current_step", {}).get("tool_calls", []),
                },
                "pending_interaction": None,
            },
        )

    def messages_to_openai(self, messages: list, *, session_id: str | None = None,
                           compact_tools: bool = False, ledger_rows: list[dict] | None = None) -> list[ChatMessage]:
        """Project business messages; runtime objects never define the storage protocol."""
        from app.agent.state import utc_now
        from app.storage.message_codec import encode_message
        rows = ledger_rows
        if rows is None:
            rows = []
            for message in messages:
                if is_internal_prompt_message(message):
                    continue
                kwargs = message.additional_kwargs or {}
                row = {"seq": len(rows) + 1,
                       "message_id": kwargs.get("ledger_message_id") or f"msg_{uuid.uuid4().hex}",
                       "turn_id": kwargs.get("ledger_turn_id") or "turn_preview",
                       "timestamp": kwargs.get("ledger_timestamp") or utc_now(),
                       **encode_message(message)}
                rows.append(row)
        return [ChatMessage(**row) for row in project_ledger(rows)["messages"]]

    async def get_history(self, session_id: str) -> SessionHistory:
        return await self._history_from_checkpoint(session_id, compact_tools=False)
