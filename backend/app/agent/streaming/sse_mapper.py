import asyncio
import uuid
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.errors import GraphInterrupt

from app.agent.content_sanitize import strip_dsml_markup
from app.agent.state import to_runtime_view, public_state_projection, to_graph_state, terminal_patch
from app.agent.streaming.events import AgentEvent
from app.context.engine import resolve_context_usage
from app.context.tool_cache import summarize_tool_result
from app.core.latency import LATENCY_STAGE_TOOL_BATCH, LATENCY_STAGE_USAGE_SNAPSHOT
from app.resilience import GRAPH_REQUEST_TIMEOUT_SECONDS
from app.tools.runtime import drain_tool_progress
from app.tools.tool_search import resolve_tool_display, should_show_tool_in_ui


def extract_final_content(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, str):
                return strip_dsml_markup(content)
            if isinstance(content, list):
                text = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                )
                return strip_dsml_markup(text)
    return ""


def _chunk_text(chunk) -> str:
    if isinstance(chunk, AIMessageChunk):
        content = chunk.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
    return ""


def _evt(event_type: str, data: dict, run_id: str | None = None) -> AgentEvent:
    return AgentEvent(type=event_type, data=data, run_id=run_id)


async def _state_snapshot(graph, config, *, status: str | None = None, run_status: str | None = None) -> dict | None:
    snapshot = await graph.aget_state(config)
    values = dict(snapshot.values) if snapshot and snapshot.values else {}
    if not values or "context" not in values:
        return None
    values["state_revision"] = int(values.get("state_revision", 0)) + 1
    values["checkpoint_id"] = values.get("checkpoint_uid") or f"cp_stream_{values['state_revision']}"
    if status:
        values["current_step"] = {**values.get("current_step", {}), "step_status": status}
    if run_status:
        values["run_status"] = run_status
    return public_state_projection(values)


async def _persist_terminal(graph, config, *, status: str, reason: str) -> dict | None:
    snapshot = await graph.aget_state(config) if hasattr(graph, "aget_state") else None
    values = dict(snapshot.values or {}) if snapshot else {}
    if "context" not in values:
        return None
    step_status = {"failed": "failed", "cancelled": "cancelled", "handoff": "skipped"}.get(status, status)
    if hasattr(graph, "aupdate_state"):
        await graph.aupdate_state(
            config,
            {
                **terminal_patch(values, status, reason),
                "current_step": {**values.get("current_step", {}), "step_status": step_status},
                "pending_interaction": None,
            },
        )
        return await _state_snapshot(graph, config, status=step_status, run_status=status)
    return public_state_projection(values)


def _lifecycle_snapshot(snapshot: dict | None, *, node: str, status: str, index: int) -> dict | None:
    if snapshot is None:
        return None
    value = dict(snapshot)
    scope = dict(value.get("scope") or {})
    scope["step_id"] = (value.get("current_step") or {}).get("step_id") or scope.get("step_id")
    value["scope"] = scope
    current = dict(value.get("current_step") or {})
    current.update({"step_id": scope["step_id"], "step_name": node, "step_status": status, "index": index})
    value["current_step"] = current
    return value


async def stream_graph_events(
    graph,
    state: dict,
    config: dict,
    max_steps: int,
    *,
    run_id: str | None = None,
    input_override=None,
) -> AsyncIterator[AgentEvent]:
    """消费 LangGraph 事件流，产出统一 AgentEvent。

    input_override: 恢复 interrupt 时传入 Command(resume=...)。
    """
    current_step = 0
    emitted_step_start = False
    full_content = ""
    active_tool_call_ids: set[str] = set()
    graph_input = input_override if input_override is not None else to_graph_state(state)

    try:
        async with asyncio.timeout(GRAPH_REQUEST_TIMEOUT_SECONDS):
            async for event in graph.astream_events(graph_input, config, version="v2"):
                kind = event.get("event", "")
                name = event.get("name", "")
                data = event.get("data", {})

                if kind == "on_chain_start" and name == "agent":
                    current_step += 1
                    emitted_step_start = True
                    state_frame = _lifecycle_snapshot(
                        await _state_snapshot(graph, config, status="running", run_status="running"),
                        node="agent", status="running", index=current_step,
                    )
                    yield AgentEvent("step_start", {"step": current_step, "max_steps": max_steps}, run_id, state_frame)

                elif kind == "on_chat_model_stream":
                    text = _chunk_text(data.get("chunk"))
                    if text:
                        text = strip_dsml_markup(text)
                        if text:
                            full_content += text
                            yield _evt("chunk", {"content": text}, run_id)

                elif kind == "on_chain_start" and name == "tools":
                    chain_input = data.get("input") or {}
                    projected_input = to_runtime_view(chain_input) if "context" in chain_input else chain_input
                    messages = projected_input.get("messages") or []
                    if messages:
                        last = messages[-1]
                        if isinstance(last, AIMessage) and last.tool_calls:
                            yield _evt(
                                "stage",
                                {
                                    "stage": LATENCY_STAGE_TOOL_BATCH,
                                    "status": "started",
                                    "tool_call_count": len(last.tool_calls),
                                },
                                run_id,
                            )
                            for tc in last.tool_calls:
                                raw_name = tc.get("name", "")
                                raw_args = tc.get("args") or {}
                                if isinstance(raw_args, str):
                                    import json

                                    try:
                                        raw_args = json.loads(raw_args)
                                    except json.JSONDecodeError:
                                        raw_args = {}
                                display_name, display_args = resolve_tool_display(raw_name, raw_args)
                                if should_show_tool_in_ui(display_name):
                                    state_frame = _lifecycle_snapshot(
                                        await _state_snapshot(graph, config, status="running"),
                                        node="tools", status="running", index=current_step,
                                    )
                                    yield AgentEvent(
                                        "tool_start",
                                        {
                                            "tool": display_name,
                                            "args": display_args,
                                            "tool_call_id": tc.get("id"),
                                        },
                                        run_id,
                                        state_frame,
                                    )
                            active_tool_call_ids.update(
                                str(tc.get("id")) for tc in last.tool_calls if tc.get("id")
                            )

                elif kind == "on_chain_end" and name == "tools":
                    session_id = config.get("configurable", {}).get("thread_id")
                    for progress in drain_tool_progress(session_id):
                        payload = dict(progress)
                        if payload.get("chunk") and not payload.get("message"):
                            payload["message"] = payload["chunk"]
                        yield _evt("tool_progress", payload, run_id)
                    output = data.get("output") or {}
                    projected_output = to_runtime_view(output) if "context" in output else output
                    todos = projected_output.get("todos")
                    if todos is not None:
                        yield _evt("todo", {"todos": todos}, run_id)
                    for msg in projected_output.get("messages") or []:
                        if isinstance(msg, ToolMessage):
                            if active_tool_call_ids and str(msg.tool_call_id or "") not in active_tool_call_ids:
                                continue
                            display_name = msg.name or "tool"
                            if should_show_tool_in_ui(display_name):
                                reason_code = (msg.additional_kwargs or {}).get("reason_code")
                                tool_result = str(msg.content) if msg.content is not None else ""
                                tool_kwargs = msg.additional_kwargs or {}
                                result_ref = tool_kwargs.get("tool_cache_path")
                                result_summary = tool_kwargs.get("tool_cache_summary") or summarize_tool_result(
                                    tool_result
                                )
                                state_frame = _lifecycle_snapshot(
                                    await _state_snapshot(
                                        graph, config,
                                        status="failed" if (msg.additional_kwargs or {}).get("reason_code") else "completed",
                                    ),
                                    node="tools",
                                    status="failed" if (msg.additional_kwargs or {}).get("reason_code") else "completed",
                                    index=current_step,
                                )
                                yield AgentEvent(
                                    "tool_finish",
                                    {
                                        "tool": display_name,
                                        "result": result_summary,
                                        "result_ref": str(result_ref) if result_ref else None,
                                        "result_truncated": result_summary != tool_result,
                                        "tool_call_id": msg.tool_call_id,
                                        "reason_code": (
                                            str(reason_code) if reason_code is not None else None
                                        ),
                                    },
                                    run_id,
                                    state_frame,
                                )
                    active_tool_call_ids.clear()
                    yield _evt(
                        "stage",
                        {
                            "stage": LATENCY_STAGE_TOOL_BATCH,
                            "status": "done",
                        },
                        run_id,
                    )

                elif kind == "on_chain_end" and name == "agent" and emitted_step_start:
                    output = data.get("output") or {}
                    session_id = config.get("configurable", {}).get("thread_id")
                    yield _evt(
                        "stage",
                        {
                            "stage": LATENCY_STAGE_USAGE_SNAPSHOT,
                            "status": "started",
                        },
                        run_id,
                    )
                    snapshot = await graph.aget_state(config)
                    values = dict(snapshot.values) if snapshot and snapshot.values else {}
                    projected_values = to_runtime_view(values) if "context" in values else values
                    projected_output = to_runtime_view(output) if "context" in output else output
                    context_usage = (
                        resolve_context_usage(projected_values, session_id)
                        if values
                        else projected_output.get("context_usage")
                    )
                    if context_usage:
                        if context_usage.get("compressing"):
                            yield _evt(
                                "compression",
                                {
                                    "status": "done",
                                    "layer": context_usage.get("compression_layer"),
                                    "message": "上下文整理完成",
                                },
                                run_id,
                            )
                        yield _evt("context_usage", context_usage, run_id)
                    api_usage = projected_output.get("api_usage")
                    if api_usage:
                        yield _evt("api_usage", api_usage, run_id)
                    session_token_stats = projected_output.get("session_token_stats")
                    if session_token_stats:
                        yield _evt("session_token_stats", session_token_stats, run_id)
                    yield _evt(
                        "stage",
                        {
                            "stage": LATENCY_STAGE_USAGE_SNAPSHOT,
                            "status": "done",
                        },
                        run_id,
                    )
                    state_frame = _lifecycle_snapshot(
                        await _state_snapshot(graph, config, status="completed"),
                        node="agent", status="completed", index=current_step,
                    )
                    yield AgentEvent("step_finish", {"step": current_step}, run_id, state_frame)
                    emitted_step_start = False

        session_id = config.get("configurable", {}).get("thread_id")
        # LangGraph 1.x：interrupt 时 astream_events 正常结束且不抛 GraphInterrupt。
        # 若此时发 done，前端会注销 run callback，随后 WS 层发出的 interrupt 会丢失。
        from app.agent.streaming.interrupts import get_pending_interrupts

        pending = await get_pending_interrupts(graph, config)
        if pending:
            raw = pending[0]
            interrupt_data = raw if isinstance(raw, dict) else {"value": raw}
            value = interrupt_data.get("value") if isinstance(interrupt_data.get("value"), dict) else interrupt_data
            snapshot = await graph.aget_state(config)
            values = dict(snapshot.values or {}) if snapshot else {}
            if "context" in values:
                state_frame = dict(values)
                state_frame["state_revision"] = int(values.get("state_revision", 0)) + 1
                state_frame["checkpoint_id"] = values.get("checkpoint_uid") or f"cp_stream_{state_frame['state_revision']}"
                state_frame["run_status"] = "waiting_user"
                state_frame["current_step"] = {**values.get("current_step", {}), "step_status": "waiting_user"}
                state_frame["pending_interaction"] = {
                    "kind": value.get("kind"), "interrupt_id": value.get("interrupt_id"),
                    "tool_call_id": value.get("tool_call_id"), "run_id": (values.get("scope") or {}).get("run_id"),
                    "step_id": (values.get("scope") or {}).get("step_id"),
                }
                state_frame = public_state_projection(state_frame)
            else:
                state_frame = None
            yield AgentEvent("interrupt", {**value, "session_id": config.get("configurable", {}).get("thread_id")}, run_id, state_frame)
            return

        final_state = await graph.aget_state(config)
        values = dict(final_state.values) if final_state and final_state.values else {}
        projected = to_runtime_view(values) if "context" in values else values
        messages = projected.get("messages", [])
        if values:
            yield _evt("context_usage", resolve_context_usage(projected, session_id), run_id)
        todos = projected.get("todos")
        if todos:
            yield _evt("todo", {"todos": todos}, run_id)
        api_usage = projected.get("api_usage")
        if api_usage:
            yield _evt("api_usage", api_usage, run_id)
        session_token_stats = projected.get("session_token_stats")
        if session_token_stats:
            yield _evt("session_token_stats", session_token_stats, run_id)
        content = extract_final_content(messages) or full_content
        terminal_reason = projected.get("terminal_reason")
        done_data: dict = {"content": content, "session_id": session_id}
        if terminal_reason:
            done_data["terminal_reason"] = terminal_reason
            # 兼容旧客户端：handoff 仍发 done，另带可区分字段
            if terminal_reason == "handoff":
                done_data["handoff"] = True
        if "context" in values and hasattr(graph, "aupdate_state"):
            terminal_status = values.get("run_status")
            if terminal_status not in {"completed", "failed", "cancelled", "handoff"}:
                terminal_status = (
                    "handoff" if terminal_reason == "handoff"
                    else "failed" if terminal_reason == "step_budget_exhausted"
                    else "completed"
                )
            await graph.aupdate_state(config, terminal_patch(values, terminal_status, terminal_reason or "completed"))
        state_frame = await _state_snapshot(
            graph, config,
            run_status=values.get("run_status") if values.get("run_status") in {"completed", "failed", "cancelled", "handoff"} else ("failed" if terminal_reason == "step_budget_exhausted" else "completed"),
        )
        if state_frame:
            state_frame["terminal_reason"] = terminal_reason or "completed"
        yield AgentEvent("done", done_data, run_id, state_frame)

    except GraphInterrupt:
        # 兼容旧路径；1.x astream_events 通常不抛，见上方 pending-interrupt 检查
        return
    except TimeoutError:
        session_id = config.get("configurable", {}).get("thread_id")
        terminal_state = await _persist_terminal(graph, config, status="failed", reason="timeout")
        yield AgentEvent(
            "error", {"error": f"请求超时（{int(GRAPH_REQUEST_TIMEOUT_SECONDS)}s）"}, run_id, terminal_state
        )
        if full_content:
            yield _evt(
                "done",
                {"content": full_content, "session_id": session_id, "partial": True},
                run_id,
            )
    except asyncio.CancelledError:
        session_id = config.get("configurable", {}).get("thread_id")
        terminal_state = await _persist_terminal(graph, config, status="cancelled", reason="cancelled")
        yield AgentEvent("error", {"error": "已取消", "cancelled": True}, run_id, terminal_state)
        yield _evt(
            "done",
            {
                "content": full_content,
                "session_id": session_id,
                "partial": True,
                "cancelled": True,
            },
            run_id,
            terminal_state,
        )
        raise
    except Exception as e:
        terminal_state = await _persist_terminal(graph, config, status="failed", reason="error")
        yield AgentEvent("error", {"error": str(e)}, run_id, terminal_state)
