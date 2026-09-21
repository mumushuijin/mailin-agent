import asyncio
import logging
import threading
import time
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.agent.approval import find_tool_card
from app.agent.approval_context import approval_enabled
from app.agent.hooks import dispatch_post_tool_call, dispatch_pre_tool_call
from app.agent.state import AgentState
from app.context.tool_cache import process_tool_message_for_cache
from app.resilience import CallContext, execute_sync, tool_error_json
from app.resilience.policies import agent_tools_batch_timeout
from app.resilience.policy import ResiliencePolicy
from app.storage.project import bind_session_runtime
from app.tools.policy import build_execution_context, evaluate_policy
from app.tools.registry import get_registry
from app.tools.runtime import tool_todos
from app.tools.tool_search import (
    TOOL_CALL_NAME,
    load_tool_search_config,
    resolve_tool_call,
    run_tool_calls,
)


logger = logging.getLogger(__name__)

ASK_USER_FAIL = "未获得用户回答。"
ASK_USER_HANDOFF = "已交还用户，当前回合结束。"


def _format_ask_user_resume(
    answer: Any,
    options: list[str],
    allow_multiple: bool,
    *,
    mode: str,
    interrupt_id: str,
) -> tuple[str | None, bool]:
    """返回 (formatted_or_none, is_handoff)."""
    if not isinstance(answer, dict) or answer.get("kind") != "ask_user":
        return None, False

    # 校验 interrupt_id，防止 stale response
    resp_iid = str(answer.get("interrupt_id") or "")
    if interrupt_id and resp_iid and resp_iid != interrupt_id:
        return None, False

    resp_mode = str(answer.get("mode") or mode or "answer_and_continue")
    if resp_mode == "handoff_and_stop" or mode == "handoff_and_stop":
        if answer.get("ack") or answer.get("answer") in (True, "ack", "ok", "确认"):
            return ASK_USER_HANDOFF, True
        return None, True

    raw = answer.get("answer")
    if allow_multiple:
        values = raw if isinstance(raw, list) else [raw] if raw not in (None, "") else []
        texts = [str(v).strip() for v in values if str(v).strip()]
        if options:
            allowed = set(options)
            texts = [t for t in texts if t in allowed]
        if not texts:
            return None, False
        return "用户选择：" + "、".join(texts), False
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None, False
    if options and text not in options:
        return None, False
    return f"用户回答：{text}", False


def _resolve_effective_card_and_args(name: str, args: dict[str, Any]):
    registry = get_registry()
    cards = registry.resolve_cards()
    if name == TOOL_CALL_NAME:
        search_config = load_tool_search_config(registry.config)
        underlying, underlying_args, err = resolve_tool_call(
            args,
            cards=cards,
            config=search_config,
        )
        if err or not underlying:
            return None, args, err or "无法解析 tool_call"
        card = next((c for c in cards if c.name == underlying), None)
        return card, underlying_args, None if card else f"未找到工具 '{underlying}'"
    card = find_tool_card(name) or next((c for c in cards if c.name == name), None)
    return card, args, None if card else None


async def call_tools(state: AgentState, config: RunnableConfig) -> dict:
    """工具节点：执行热工具 / 桥接工具；策略 gate 在 handler 前做权威判定。"""
    session_id = config.get("configurable", {}).get("thread_id", "default")
    run_id = config.get("configurable", {}).get("run_id")
    project_workspace = bind_session_runtime(session_id)

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
    terminal_reason: str | None = None

    for tc in tool_calls:
        if terminal_reason == "handoff":
            # handoff 后不执行排队的副作用工具
            denied_messages.append(
                ToolMessage(
                    content="当前回合已交还用户，跳过后续工具调用。",
                    tool_call_id=tc.get("id") or "",
                    name=tc.get("name") or "tool",
                    additional_kwargs={
                        "tool_status": "skipped",
                        "reason_code": "skipped_after_handoff",
                    },
                )
            )
            continue

        name = tc.get("name", "")
        tc_id = tc.get("id") or ""
        tc_args = tc.get("args") or {}
        if isinstance(tc_args, str):
            tc_args = {}

        # 钩子保留为观察/兼容层；内置 Shell hard deny 已迁到 policy gate
        block_reason = dispatch_pre_tool_call(
            tool_name=name,
            args=tc_args,
            tool_call_id=tc_id,
            session_id=session_id,
        )
        if block_reason:
            denied_messages.append(
                ToolMessage(
                    content=block_reason,
                    tool_call_id=tc_id,
                    name=name,
                    additional_kwargs={
                        "tool_status": "failed",
                        "reason_code": "pre_tool_blocked",
                    },
                )
            )
            continue

        if name == "ask_user":
            import uuid as _uuid

            options_raw = tc_args.get("options") or []
            options = [str(x) for x in options_raw] if isinstance(options_raw, list) else []
            allow_multiple = bool(tc_args.get("allow_multiple"))
            mode = str(tc_args.get("mode") or "answer_and_continue").strip()
            if mode not in {"answer_and_continue", "handoff_and_stop"}:
                mode = "answer_and_continue"
            interrupt_id = str(tc_args.get("interrupt_id") or _uuid.uuid4().hex)
            terminal_on_ack = mode == "handoff_and_stop"
            resume = interrupt(
                {
                    "kind": "ask_user",
                    "prompt": str(tc_args.get("question") or tc_args.get("prompt") or ""),
                    "options": options,
                    "allow_multiple": allow_multiple,
                    "tool_call_id": tc_id,
                    "interrupt_id": interrupt_id,
                    "mode": mode,
                    "terminal_on_ack": terminal_on_ack,
                    "run_id": run_id,
                    "session_id": session_id,
                }
            )
            formatted, is_handoff = _format_ask_user_resume(
                resume,
                options,
                allow_multiple,
                mode=mode,
                interrupt_id=interrupt_id,
            )
            denied_messages.append(
                ToolMessage(
                    content=formatted or ASK_USER_FAIL,
                    tool_call_id=tc_id,
                    name=name,
                    additional_kwargs={
                        "tool_status": "completed" if formatted else "failed",
                        "reason_code": (
                            "handoff"
                            if is_handoff and formatted
                            else "ask_user_answered"
                            if formatted
                            else "ask_user_unanswered"
                        ),
                    },
                )
            )
            if is_handoff and formatted:
                terminal_reason = "handoff"
            continue

        card, effective_args, resolve_err = _resolve_effective_card_and_args(name, tc_args)
        if resolve_err:
            denied_messages.append(
                ToolMessage(
                    content=resolve_err,
                    tool_call_id=tc_id,
                    name=name,
                    additional_kwargs={
                        "tool_status": "failed",
                        "reason_code": "tool_resolution_error",
                    },
                )
            )
            continue

        allowlist_matched = False
        shell_preview = None
        if card and card.name == "run_shell":
            from app.tools.packages.shell.approval import (
                build_shell_approval_preview,
                should_auto_approve_run_shell,
            )

            cmd = str(effective_args.get("command") or "")
            cwd_arg = effective_args.get("workdir") or effective_args.get("cwd")
            bg = bool(effective_args.get("background"))
            timeout_arg = effective_args.get("timeout")
            allowlist_matched = should_auto_approve_run_shell(
                cmd,
                cwd=str(cwd_arg) if cwd_arg else None,
                background=bg,
                timeout_seconds=float(timeout_arg) if timeout_arg is not None else None,
                session_id=session_id,
            )
            shell_preview = build_shell_approval_preview(
                cmd,
                cwd=str(cwd_arg) if cwd_arg else None,
                background=bg,
                timeout_seconds=float(timeout_arg) if timeout_arg is not None else None,
                session_id=session_id,
            )

        if card is not None:
            ctx = build_execution_context(
                card,
                effective_args,
                tool_call_id=tc_id,
                session_id=session_id,
                run_id=run_id,
                approval_granted=False,
                allowlist_matched=allowlist_matched,
                workspace_root=project_workspace,
            )
            decision = evaluate_policy(ctx)
            if decision.outcome == "deny":
                denied_messages.append(
                    ToolMessage(
                        content=decision.to_tool_error_message(),
                        tool_call_id=tc_id,
                        name=card.name if card else name,
                        additional_kwargs={
                            "tool_status": "failed",
                            "reason_code": decision.reason_code or "policy_denied",
                        },
                    )
                )
                continue
            if decision.outcome == "needs_approval" and approval_enabled.get():
                preview = decision.preview.to_public_dict() if decision.preview else {}
                if shell_preview:
                    preview.update(shell_preview)
                user_decision = interrupt(
                    {
                        "kind": "approval",
                        "tool": card.name,
                        "args": effective_args,
                        "tool_call_id": tc_id,
                        "reason": decision.reason,
                        "reason_code": decision.reason_code,
                        "preview": preview,
                        "run_id": run_id,
                        "session_id": session_id,
                    }
                )
                if user_decision != "allow":
                    denied_messages.append(
                        ToolMessage(
                            content="用户拒绝了此工具调用。",
                            tool_call_id=tc_id,
                            name=card.name,
                            additional_kwargs={
                                "tool_status": "failed",
                                "reason_code": "user_denied",
                            },
                        )
                    )
                    continue
                # 审批后重新校验硬边界（路径/策略可能在等待期间变化）
                recheck = evaluate_policy(
                    build_execution_context(
                        card,
                        effective_args,
                        tool_call_id=tc_id,
                        session_id=session_id,
                        run_id=run_id,
                        approval_granted=True,
                        allowlist_matched=allowlist_matched,
                        workspace_root=project_workspace,
                    )
                )
                if recheck.outcome == "deny":
                    denied_messages.append(
                        ToolMessage(
                            content=recheck.to_tool_error_message(),
                            tool_call_id=tc_id,
                            name=card.name,
                            additional_kwargs={
                                "tool_status": "failed",
                                "reason_code": recheck.reason_code or "policy_denied",
                            },
                        )
                    )
                    continue

        approved_calls.append(tc)

    result: dict[str, Any] = {"todos": todos}
    if terminal_reason:
        result["terminal_reason"] = terminal_reason

    if not approved_calls:
        result["messages"] = denied_messages
        return result

    cancel_event = threading.Event()

    def _run() -> tuple[list[ToolMessage], list]:
        bind_session_runtime(session_id)
        token = tool_todos.set(list(todos))
        try:
            msgs = run_tool_calls(
                approved_calls,
                session_id,
                process_for_cache=process_tool_message_for_cache,
                run_id=run_id,
                timeout_seconds=max(0.1, batch_timeout - 0.25),
                cancel_event=cancel_event,
            )
            return msgs, list(tool_todos.get() or [])
        finally:
            tool_todos.reset(token)

    def _fallback(
        *,
        reason_code: str = "tool_error",
        message: str = "工具执行失败，请稍后重试",
    ) -> tuple[list[ToolMessage], list]:
        logger.warning(
            "工具节点执行失败，为 %d 个 tool_call 返回错误 reason=%s",
            len(approved_calls),
            reason_code,
        )
        return (
            [
                ToolMessage(
                    content=tool_error_json(message),
                    tool_call_id=tc.get("id") or "",
                    name=tc.get("name") or "tool",
                    additional_kwargs={
                        "tool_status": "failed",
                        "reason_code": reason_code,
                    },
                )
                for tc in approved_calls
            ],
            todos,
        )

    started = time.monotonic()
    batch_timeout = agent_tools_batch_timeout(len(approved_calls))
    try:
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
    except asyncio.CancelledError:
        cancel_event.set()
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    executed: list[ToolMessage]
    if outcome.ok and outcome.value is not None:
        executed, todos = outcome.value
    else:
        error_text = str(outcome.error or "")
        is_timeout = "超时" in error_text or "timeout" in error_text.lower()
        executed, todos = _fallback(
            reason_code="batch_timeout" if is_timeout else "tool_error",
            message="工具批次超时，请稍后重试" if is_timeout else "工具执行失败，请稍后重试",
        )

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

    return {
        "messages": denied_messages + executed,
        "todos": todos,
        **({"terminal_reason": terminal_reason} if terminal_reason else {}),
    }
