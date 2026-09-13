import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agent.messages import is_system_maintenance
from app.agent.graph import get_graph, make_initial_state, make_thread_config
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
from app.maintenance.runner import get_maintenance_runner
from app.core.llm import get_chat_model, load_agent_config
from app.core.settings import get_settings
from app.resilience import GRAPH_REQUEST_TIMEOUT_SECONDS
from app.schemas.chat import ChatResponse
from app.schemas.session import ChatMessage, SessionHistory, ToolCall, ToolCallFunction
from app.core.exceptions import AppError
from app.storage.project import require_bound_session
from app.storage.workspace import SessionStore
from app.tools.registry import load_full_config
from app.tools.tool_search import resolve_tool_display, should_show_tool_in_ui


class ChatService:
    def __init__(self):
        self.settings = get_settings()
        self.session_store = SessionStore(self.settings.workspace_path)

    def _max_steps(self) -> int:
        config = load_full_config(self.settings.workspace_path)
        return config.get("agent", {}).get("max_steps", 24)

    def _require_bound_session(self, session_id: str | None) -> str:
        sid, _project = require_bound_session(session_id)
        return sid

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

        _ = run_id  # 日志上下文由 API 层 log_scope 绑定

        sid = self._require_bound_session(session_id)

        if not get_settings().has_llm:
            raise RuntimeError("未配置 LLM API Key")
        self.session_store.maybe_set_title_from_message(sid, message)
        await prepare_session_memory(message)
        graph = await asyncio.to_thread(get_graph)
        config = make_thread_config(sid)
        max_steps = self._max_steps()
        state = make_initial_state(message, max_steps)

        try:
            result = await asyncio.wait_for(
                graph.ainvoke(state, config),
                timeout=GRAPH_REQUEST_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"请求超时（{int(GRAPH_REQUEST_TIMEOUT_SECONDS)}s），请稍后重试"
            ) from exc
        content = extract_final_content(result.get("messages", []))
        await self._refine_auto_title_after_turn(sid, message, content)
        dispatch_post_llm_call(
            session_id=sid,
            user_message=message,
            assistant_response=content,
        )
        dispatch_observe(ON_SESSION_END, session_id=sid, completed=True, interrupted=False)
        if result.get("memory_nudge_pending"):
            await get_maintenance_runner().run_memory_nudge_for_session(sid)
        self.session_store.touch(sid)
        return ChatResponse(content=content, session_id=sid)

    async def iter_chat_events(
        self,
        message: str,
        session_id: str | None,
        *,
        run_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """聊天主流程：产出统一 AgentEvent 流（SSE / WS 共用）。"""
        try:
            sid = self._require_bound_session(session_id)
        except AppError as exc:
            yield AgentEvent("error", {"error": exc.message}, run_id)
            return

        if not self.settings.has_llm:
            yield AgentEvent("error", {"error": "未配置 LLM API Key，请在 backend/.env 设置 OPENAI_API_KEY"}, run_id)
            return
        yield AgentEvent("session", {"session_id": sid}, run_id)
        self.session_store.maybe_set_title_from_message(sid, message)
        await prepare_session_memory(message)

        graph = await asyncio.to_thread(get_graph)
        config = make_thread_config(sid)
        max_steps = self._max_steps()
        state = make_initial_state(message, max_steps)

        nudge_pending = False
        final_content = ""
        completed = False
        async for event in stream_graph_events(graph, state, config, max_steps, run_id=run_id):
            yield event
            if event.type == "done":
                self.session_store.touch(sid)
                final_content = event.data.get("content", "") or ""
                completed = not event.data.get("partial", False)
                snapshot = await graph.aget_state(config)
                values = dict(snapshot.values) if snapshot and snapshot.values else {}
                nudge_pending = bool(values.get("memory_nudge_pending"))

        if completed:
            asyncio.create_task(self._refine_auto_title_after_turn(sid, message, final_content))
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
            await get_maintenance_runner().run_memory_nudge_for_session(sid)

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
        graph = await asyncio.to_thread(get_graph)
        if await has_pending_interrupt(graph, make_thread_config(sid)):
            yield to_sse(
                AgentEvent(
                    "error",
                    {
                        "error": "此操作需要在 WebSocket 连接下确认或回答，请重连后重试",
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

        graph = await asyncio.to_thread(get_graph)
        config = make_thread_config(session_id)
        max_steps = self._max_steps()

        async for event in stream_graph_events(
            graph,
            {},
            config,
            max_steps,
            run_id=run_id,
            input_override=Command(resume=decision),
        ):
            yield event
            if event.type == "done":
                self.session_store.touch(session_id)

    async def repair_cancelled_checkpoint(self, session_id: str) -> None:
        """取消时修复未完成的 tool_calls。"""
        from app.context.ledger import cancel_tool_calls_message, has_pending_tool_calls

        graph = await asyncio.to_thread(get_graph)
        config = make_thread_config(session_id)
        snapshot = await graph.aget_state(config)
        if not snapshot or not snapshot.values:
            return
        messages = list(snapshot.values.get("messages") or [])
        if not has_pending_tool_calls(messages):
            return
        last = messages[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            cancelled = cancel_tool_calls_message(last)
            await graph.aupdate_state(config, {"messages": cancelled})

    def _message_timestamp(self, msg: BaseMessage) -> int | None:
        ts = msg.additional_kwargs.get("timestamp")
        if isinstance(ts, (int, float)):
            return int(ts)
        return None

    def messages_to_openai(self, messages: list) -> list[ChatMessage]:
        import json

        tool_display_names: dict[str, str] = {}
        for msg in messages:
            if isinstance(msg, ToolMessage) and msg.tool_call_id and msg.name:
                tool_display_names[msg.tool_call_id] = msg.name

        result: list[ChatMessage] = []
        for msg in messages:
            kwargs = getattr(msg, "additional_kwargs", None) or {}
            if kwargs.get("system_maintenance"):
                continue
            if isinstance(msg, HumanMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                result.append(
                    ChatMessage(role="user", content=content, timestamp=self._message_timestamp(msg))
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
                result.append(
                    ChatMessage(
                        role="assistant",
                        content=content or None,
                        tool_calls=tool_calls,
                        timestamp=self._message_timestamp(msg),
                    )
                )
            elif isinstance(msg, ToolMessage):
                display_name = msg.name or "tool"
                if not should_show_tool_in_ui(display_name):
                    continue
                result.append(
                    ChatMessage(
                        role="tool",
                        content=str(msg.content),
                        tool_call_id=msg.tool_call_id,
                        timestamp=self._message_timestamp(msg),
                    )
                )
        return result

    async def get_history(self, session_id: str) -> SessionHistory:
        self.session_store.get(session_id)
        graph = await asyncio.to_thread(get_graph)
        config = make_thread_config(session_id)
        state = await graph.aget_state(config)
        values = dict(state.values) if state and state.values else {}
        messages = repair_orphan_tool_calls(values.get("messages", []))
        session_token_stats = values.get("session_token_stats") or {}
        api_usage = values.get("api_usage") or {}
        context_usage = resolve_context_usage({**values, "messages": messages}, session_id)

        return SessionHistory(
            session_id=session_id,
            messages=self.messages_to_openai(messages),
            context_usage=context_usage,
            api_usage=api_usage or None,
            session_token_stats=session_token_stats or None,
            todos=list(values.get("todos") or []) or None,
        )
