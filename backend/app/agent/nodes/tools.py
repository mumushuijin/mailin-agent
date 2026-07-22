import asyncio
import logging
import time

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.agent.approval import needs_approval
from app.agent.approval_context import approval_enabled
from app.agent.hooks import dispatch_post_tool_call, dispatch_pre_tool_call
from app.agent.state import AgentState
from app.context.tool_cache import SKIP_CACHE_KWARG, is_tool_results_path, process_tool_message_for_cache
from app.resilience import CallContext, execute_sync, tool_error_json
from app.resilience.policies import agent_tools_batch_timeout
from app.resilience.policy import ResiliencePolicy
from app.tools.runtime import set_tool_session
from app.tools.tool_search import run_tool_calls

logger = logging.getLogger(__name__)


async def call_tools(state: AgentState, config: RunnableConfig) -> dict:
    """工具节点：执行热工具 / 桥接工具，敏感工具需用户审批。"""
    session_id = config.get("configurable", {}).get("thread_id", "default")
    set_tool_session(session_id)

    messages = state.get("messages") or []
    if not messages:
        return {"messages": []}

    last = messages[-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"messages": []}

    tool_calls = list(last.tool_calls)
    approved_calls = []
    denied_messages: list[ToolMessage] = []

    for tc in tool_calls:
        name = tc.get("name", "")
        tc_id = tc.get("id") or ""
        tc_args = tc.get("args") or {}

        # pre_tool_call：程序化硬规则先于人工审批。首个 block 生效。
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

        if needs_approval(name) and approval_enabled.get():
            if name == "run_shell":
                from app.tools.packages.shell.approval import should_auto_approve_run_shell

                if should_auto_approve_run_shell(str(tc_args.get("command") or "")):
                    approved_calls.append(tc)
                    continue
            decision = interrupt(
                {
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
        return {"messages": denied_messages}

    def _run() -> list[ToolMessage]:
        return run_tool_calls(
            approved_calls,
            session_id,
            process_for_cache=process_tool_message_for_cache,
        )

    def _fallback() -> list[ToolMessage]:
        logger.warning("工具节点执行失败，为 %d 个 tool_call 返回错误", len(approved_calls))
        return [
            ToolMessage(
                content=tool_error_json("工具执行失败，请稍后重试"),
                tool_call_id=tc.get("id") or "",
                name=tc.get("name") or "tool",
            )
            for tc in approved_calls
        ]

    started = time.monotonic()
    batch_timeout = agent_tools_batch_timeout(len(approved_calls))
    # 在线程池中执行，避免阻塞事件循环导致 WS 取消/心跳/维护推送卡死
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
    executed: list[ToolMessage] = []
    if outcome.ok and outcome.value is not None:
        executed = outcome.value
    else:
        executed = _fallback()

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

    return {"messages": denied_messages + executed}
