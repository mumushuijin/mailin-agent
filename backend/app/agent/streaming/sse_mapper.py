import asyncio
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.errors import GraphInterrupt

from app.agent.content_sanitize import strip_dsml_markup
from app.agent.streaming.events import AgentEvent
from app.context.engine import resolve_context_usage
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
    graph_input = input_override if input_override is not None else state

    try:
        async with asyncio.timeout(GRAPH_REQUEST_TIMEOUT_SECONDS):
            async for event in graph.astream_events(graph_input, config, version="v2"):
                kind = event.get("event", "")
                name = event.get("name", "")
                data = event.get("data", {})

                if kind == "on_chain_start" and name == "agent":
                    current_step += 1
                    emitted_step_start = True
                    yield _evt("step_start", {"step": current_step, "max_steps": max_steps}, run_id)

                elif kind == "on_chat_model_stream":
                    text = _chunk_text(data.get("chunk"))
                    if text:
                        text = strip_dsml_markup(text)
                        if text:
                            full_content += text
                            yield _evt("chunk", {"content": text}, run_id)

                elif kind == "on_chain_start" and name == "tools":
                    chain_input = data.get("input") or {}
                    messages = chain_input.get("messages") or []
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
                                    yield _evt(
                                        "tool_start",
                                        {
                                            "tool": display_name,
                                            "args": display_args,
                                            "tool_call_id": tc.get("id"),
                                        },
                                        run_id,
                                    )

                elif kind == "on_chain_end" and name == "tools":
                    session_id = config.get("configurable", {}).get("thread_id")
                    for progress in drain_tool_progress(session_id):
                        payload = dict(progress)
                        if payload.get("chunk") and not payload.get("message"):
                            payload["message"] = payload["chunk"]
                        yield _evt("tool_progress", payload, run_id)
                    output = data.get("output") or {}
                    todos = output.get("todos")
                    if todos is not None:
                        yield _evt("todo", {"todos": todos}, run_id)
                    for msg in output.get("messages") or []:
                        if isinstance(msg, ToolMessage):
                            display_name = msg.name or "tool"
                            if should_show_tool_in_ui(display_name):
                                reason_code = (msg.additional_kwargs or {}).get("reason_code")
                                yield _evt(
                                    "tool_finish",
                                    {
                                        "tool": display_name,
                                        "result": str(msg.content) if msg.content is not None else "",
                                        "tool_call_id": msg.tool_call_id,
                                        "reason_code": (
                                            str(reason_code) if reason_code is not None else None
                                        ),
                                    },
                                    run_id,
                                )
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
                    context_usage = (
                        resolve_context_usage(values, session_id)
                        if values
                        else output.get("context_usage")
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
                    api_usage = output.get("api_usage")
                    if api_usage:
                        yield _evt("api_usage", api_usage, run_id)
                    session_token_stats = output.get("session_token_stats")
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
                    yield _evt("step_finish", {"step": current_step}, run_id)
                    emitted_step_start = False

        session_id = config.get("configurable", {}).get("thread_id")
        # LangGraph 1.x：interrupt 时 astream_events 正常结束且不抛 GraphInterrupt。
        # 若此时发 done，前端会注销 run callback，随后 WS 层发出的 interrupt 会丢失。
        from app.agent.streaming.interrupts import has_pending_interrupt

        if await has_pending_interrupt(graph, config):
            return

        final_state = await graph.aget_state(config)
        values = dict(final_state.values) if final_state and final_state.values else {}
        messages = values.get("messages", [])
        if values:
            yield _evt("context_usage", resolve_context_usage(values, session_id), run_id)
        todos = values.get("todos")
        if todos:
            yield _evt("todo", {"todos": todos}, run_id)
        api_usage = values.get("api_usage")
        if api_usage:
            yield _evt("api_usage", api_usage, run_id)
        session_token_stats = values.get("session_token_stats")
        if session_token_stats:
            yield _evt("session_token_stats", session_token_stats, run_id)
        content = extract_final_content(messages) or full_content
        terminal_reason = values.get("terminal_reason")
        done_data: dict = {"content": content, "session_id": session_id}
        if terminal_reason:
            done_data["terminal_reason"] = terminal_reason
            # 兼容旧客户端：handoff 仍发 done，另带可区分字段
            if terminal_reason == "handoff":
                done_data["handoff"] = True
        yield _evt("done", done_data, run_id)

    except GraphInterrupt:
        # 兼容旧路径；1.x astream_events 通常不抛，见上方 pending-interrupt 检查
        return
    except TimeoutError:
        session_id = config.get("configurable", {}).get("thread_id")
        yield _evt(
            "error",
            {"error": f"请求超时（{int(GRAPH_REQUEST_TIMEOUT_SECONDS)}s）"},
            run_id,
        )
        if full_content:
            yield _evt(
                "done",
                {"content": full_content, "session_id": session_id, "partial": True},
                run_id,
            )
    except asyncio.CancelledError:
        session_id = config.get("configurable", {}).get("thread_id")
        yield _evt("error", {"error": "已取消", "cancelled": True}, run_id)
        yield _evt(
            "done",
            {
                "content": full_content,
                "session_id": session_id,
                "partial": True,
                "cancelled": True,
            },
            run_id,
        )
        raise
    except Exception as e:
        yield _evt("error", {"error": str(e)}, run_id)
