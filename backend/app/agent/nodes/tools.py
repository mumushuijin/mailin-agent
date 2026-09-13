import asyncio
import logging
import time
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.agent.approval import needs_approval
from app.agent.approval_context import approval_enabled
from app.agent.hooks import dispatch_post_tool_call, dispatch_pre_tool_call
from app.agent.state import AgentState
from app.context.tool_cache import process_tool_message_for_cache
from app.resilience import CallContext, execute_sync, tool_error_json
from app.resilience.policies import agent_tools_batch_timeout
from app.resilience.policy import ResiliencePolicy
from app.storage.project import bind_session_runtime
from app.tools.runtime import tool_todos
from app.tools.tool_search import run_tool_calls

logger = logging.getLogger(__name__)

ASK_USER_FAIL = "未获得用户回答。"


def _format_ask_user_resume(answer: Any, options: list[str], allow_multiple: bool) -> str | None:
    if not isinstance(answer, dict) or answer.get("kind") != "ask_user":
        return None
    raw = answer.get("answer")
    if allow_multiple:
        values = raw if isinstance(raw, list) else [raw] if raw not in (None, "") else []
        texts = [str(v).strip() for v in values if str(v).strip()]
        if options:
            allowed = set(options)
            texts = [t for t in texts if t in allowed]
        if not texts:
            return None
        return "用户选择：" + "、".join(texts)
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    if options and text not in options:
        return None
    return f"用户回答：{text}"


async def call_tools(state: AgentState, config: RunnableConfig) -> dict:
    """工具节点：执行热工具 / 桥接工具，敏感工具需用户审批。"""
    session_id = config.get("configurable", {}).get("thread_id", "default")
    bind_session_runtime(session_id)

    messages = state.get("messages") or []
    if not messages:
        return {"messages": []}

    last = messages[-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"messages": []}

    tool_calls = list(last.tool_calls)
    approved_calls = []
    denied_messages: list[ToolMessage] = []
    todos = list(state.get("todos") or [])

    for tc in tool_calls:
        name = tc.get("name", "")
        tc_id = tc.get("id") or ""
        tc_args = tc.get("args") or {}

        block_reason = dispatch_pre_tool_call(
            tool_name=name,
            args=tc_args,
            tool_call_id=tc_id,
            session_id=session_id,
        )
        if block_reason:
            denied_messages.append(
                ToolMessage(content=block_reason, tool_call_id=tc_id, name=name)
            )
            continue

        if name == "ask_user":
            options_raw = tc_args.get("options") or []
            options = [str(x) for x in options_raw] if isinstance(options_raw, list) else []
            allow_multiple = bool(tc_args.get("allow_multiple"))
            resume = interrupt(
                {
                    "kind": "ask_user",
                    "prompt": str(tc_args.get("question") or tc_args.get("prompt") or ""),
                    "options": options,
                    "allow_multiple": allow_multiple,
                    "tool_call_id": tc_id,
                }
            )
            formatted = _format_ask_user_resume(resume, options, allow_multiple)
            denied_messages.append(
                ToolMessage(
                    content=formatted or ASK_USER_FAIL,
                    tool_call_id=tc_id,
                    name=name,
                )
            )
            continue

        if needs_approval(name) and approval_enabled.get():
            if name == "run_shell":
                from app.tools.packages.shell.approval import should_auto_approve_run_shell

                if should_auto_approve_run_shell(str(tc_args.get("command") or "")):
                    approved_calls.append(tc)
                    continue
            decision = interrupt(
                {
                    "kind": "approval",
                    "tool": name,
                    "args": tc.get("args") or {},
                    "tool_call_id": tc_id,
                    "reason": f"工具「{name}」需要确认后执行",
                }
            )
            if decision != "allow":
                denied_messages.append(
                    ToolMessage(
                        content="用户拒绝了此工具调用。",
                        tool_call_id=tc_id,
                        name=name,
                    )
                )
                continue
        approved_calls.append(tc)

    if not approved_calls:
        return {"messages": denied_messages, "todos": todos}

    def _run() -> tuple[list[ToolMessage], list]:
        bind_session_runtime(session_id)
        token = tool_todos.set(list(todos))
        try:
            msgs = run_tool_calls(
                approved_calls,
                session_id,
                process_for_cache=process_tool_message_for_cache,
            )
            return msgs, list(tool_todos.get() or [])
        finally:
            tool_todos.reset(token)

    def _fallback() -> tuple[list[ToolMessage], list]:
        logger.warning("工具节点执行失败，为 %d 个 tool_call 返回错误", len(approved_calls))
        return (
            [
                ToolMessage(
                    content=tool_error_json("工具执行失败，请稍后重试"),
                    tool_call_id=tc.get("id") or "",
                    name=tc.get("name") or "tool",
                )
                for tc in approved_calls
            ],
            todos,
        )

    started = time.monotonic()
    batch_timeout = agent_tools_batch_timeout(len(approved_calls))
    outcome = await asyncio.to_thread(
        execute_sync,
        "agent.tools",
        _run,
        policy=ResiliencePolicy(
            dependency_id="agent.tools",
            timeout_seconds=batch_timeout,
            max_retries=0,
            fallback=_fallback,
        ),
        context=CallContext(session_id=session_id, metadata={"tool_calls": len(approved_calls)}),
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    executed: list[ToolMessage]
    if outcome.ok and outcome.value is not None:
        executed, todos = outcome.value
    else:
        executed, todos = _fallback()

    args_by_id = {tc.get("id") or "": (tc.get("args") or {}) for tc in approved_calls}
    for msg in executed:
        dispatch_post_tool_call(
            tool_name=msg.name or "tool",
            args=args_by_id.get(msg.tool_call_id or "", {}),
            result=str(msg.content) if msg.content is not None else "",
            tool_call_id=msg.tool_call_id or "",
            session_id=session_id,
            duration_ms=duration_ms,
        )

    return {"messages": denied_messages + executed, "todos": todos}
