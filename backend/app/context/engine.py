from __future__ import annotations

import logging

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.context.bootstrap import load_bootstrap
from app.context.budget import (
    build_usage_report,
    compute_mixed_signal,
    count_message_tokens,
    count_messages_tokens,
    estimate_tokens,
    load_context_config,
    resolve_effective_tokens,
    threshold_tokens,
)
from app.context.compression_layers import CompressionLayer
from app.context.compressor import (
    apply_layer_a_tail_tool_summary,
    apply_layer_b_old_tool_oneline,
    build_reference_block,
    compress_working_messages,
    emergency_compress,
    sanitize_working_messages,
)
from app.context.ledger import (
    get_current_user_message,
    get_ledger_messages,
    has_pending_tool_calls,
    repair_orphan_tool_calls,
    split_tail_window,
)
from app.context.schemas import AssembleResult, ContextBreakdown
from app.context.sediment import build_memory_hints, should_increment_memory_turn
from app.context.skills import load_skills_catalog
from app.core.settings import get_settings
from app.resilience import context_step_policy, guarded_step
from app.tools.context import build_tools_disclosure_context

logger = logging.getLogger(__name__)

_DEFAULT_BOOTSTRAP = (
    "你是麦林（Mailin），理性而温暖的数字伙伴。"
    "帮助用户推进事情、理解世界、完成创造。使用中文交流，必要时调用工具。"
)


def _bootstrap_fallback() -> tuple[str, int]:
    return _DEFAULT_BOOTSTRAP, estimate_tokens(_DEFAULT_BOOTSTRAP)


def _skills_fallback() -> tuple[str, int]:
    return "", 0


def _memory_hints_fallback(state: dict) -> tuple[str, int, bool]:
    return "", int(state.get("memory_turn_counter") or 0), False


def _tools_disclosure_fallback() -> str:
    return ""


def _load_context_parts(
    state: dict,
    ledger: list[BaseMessage],
    *,
    session_id: str | None,
    measure_only: bool,
) -> tuple[str, int, str, int, str, int, str, bool]:
    bootstrap_text, bootstrap_tokens = guarded_step(
        "context.bootstrap",
        load_bootstrap,
        _bootstrap_fallback,
        session_id=session_id,
        policy=context_step_policy("bootstrap", fallback=_bootstrap_fallback),
    )
    skills_text, skills_tokens = guarded_step(
        "context.skills",
        load_skills_catalog,
        _skills_fallback,
        session_id=session_id,
        policy=context_step_policy("skills", fallback=_skills_fallback),
    )
    memory_hints, memory_turn_counter, memory_nudge_pending = guarded_step(
        "context.memory_hints",
        lambda: build_memory_hints(
            state,
            get_settings().workspace_path,
            increment_turn=(
                not measure_only and should_increment_memory_turn(ledger)
            ),
        ),
        lambda: _memory_hints_fallback(state),
        session_id=session_id,
        policy=context_step_policy(
            "memory_hints",
            fallback=lambda: _memory_hints_fallback(state),
        ),
    )
    tools_disclosure = guarded_step(
        "context.tools_disclosure",
        build_tools_disclosure_context,
        _tools_disclosure_fallback,
        session_id=session_id,
        policy=context_step_policy("tools_disclosure", fallback=_tools_disclosure_fallback),
    )
    return (
        bootstrap_text,
        bootstrap_tokens,
        skills_text,
        skills_tokens,
        memory_hints,
        memory_turn_counter,
        tools_disclosure,
        memory_nudge_pending,
    )


def _build_head_messages(
    bootstrap_text: str,
    *,
    skills_catalog: str,
    memory_hints: str,
    tools_disclosure: str,
    context_summary: str,
) -> tuple[list[BaseMessage], int]:
    """窗口头部：仅 bootstrap 为 SystemMessage，其余为参考信息块。"""
    system_message = SystemMessage(content=bootstrap_text)
    reference = build_reference_block(
        skills_catalog=skills_catalog,
        memory_hints=memory_hints,
        tools_disclosure=tools_disclosure,
        context_summary=context_summary,
    )
    head: list[BaseMessage] = [system_message]
    if reference is not None:
        head.append(reference)
    head_tokens = count_messages_tokens(head)
    return head, head_tokens


def _count_tool_tokens(messages: list[BaseMessage]) -> int:
    return sum(count_message_tokens(m) for m in messages if isinstance(m, ToolMessage))


def assemble_context(
    state: dict,
    session_id: str | None = None,
    *,
    emergency: bool = False,
    measure_only: bool = False,
) -> AssembleResult:
    """
    每轮 call_agent 前执行：从账本派生工作上下文，计量 token，必要时压缩。
    账本（state.messages）不会被修改。

    窗口结构（lost-in-the-middle）：
      头部（不压缩）：bootstrap SystemMessage + 参考信息块
      中间：历史对话（超限时压缩为摘要）
      尾部：最近 20k tokens 内消息（仅对超长工具结果做 Layer A）
    """
    config = load_context_config()
    max_tokens = int(config.get("max_tokens", 128_000))
    threshold_ratio = float(config.get("compress_threshold_ratio", 0.75))
    budgets = config.get("budget", {})
    reserved_tokens = int(max_tokens * budgets.get("reserved", 0.08))
    tail_budget = int(config.get("recent_tail_max_tokens", 20_000))
    threshold = threshold_tokens(max_tokens, threshold_ratio)

    ledger = repair_orphan_tool_calls(get_ledger_messages(state))
    existing_summary = state.get("context_summary") or ""
    compression_count = int(state.get("compression_count") or 0)
    compression_api_usages: list[dict] = []

    (
        bootstrap_text,
        bootstrap_tokens,
        skills_text,
        skills_tokens,
        memory_hints,
        memory_turn_counter,
        tools_disclosure,
        memory_nudge_pending,
    ) = _load_context_parts(state, ledger, session_id=session_id, measure_only=measure_only)

    head, head_tokens = _build_head_messages(
        bootstrap_text,
        skills_catalog=skills_text,
        memory_hints=memory_hints,
        tools_disclosure=tools_disclosure,
        context_summary=existing_summary if not emergency else "",
    )

    if emergency:
        def _emergency_fallback() -> tuple[list[BaseMessage], list[dict]]:
            logger.warning("紧急压缩失败，回退为截断最近消息")
            _, tail = split_tail_window(ledger, tail_budget)
            return head + tail[-3:], []

        compressed, emergency_usages = guarded_step(
            "context.emergency_compress",
            lambda: emergency_compress(ledger, head_messages=head, session_id=session_id),
            _emergency_fallback,
            session_id=session_id,
            policy=context_step_policy("emergency_compress", fallback=_emergency_fallback),
        )
        compression_api_usages.extend(emergency_usages)
        working = sanitize_working_messages(compressed)
        breakdown = ContextBreakdown(
            bootstrap=bootstrap_tokens,
            skills=skills_tokens,
            summary=count_messages_tokens(
                [m for m in working if isinstance(m, HumanMessage) and (m.additional_kwargs or {}).get("context_summary")]
            ),
            recent_turns=count_messages_tokens(
                [m for m in working if not isinstance(m, SystemMessage)]
            ),
            reserved=reserved_tokens,
        )
        local_estimate = breakdown.total
        effective = resolve_effective_tokens(
            state,
            ledger,
            head_tokens=head_tokens,
            reserved_tokens=reserved_tokens,
            local_estimate=local_estimate,
        )
        usage = build_usage_report(
            breakdown,
            max_tokens,
            compression_layer=int(CompressionLayer.EMERGENCY),
            warning="紧急压缩",
            effective_tokens=effective,
        )
        return AssembleResult(
            messages=working,
            usage=usage,
            context_summary=existing_summary,
            compression_count=compression_count,
            compressing=True,
            compression_layer=int(CompressionLayer.EMERGENCY),
            compression_api_usages=compression_api_usages,
            memory_turn_counter=memory_turn_counter,
            memory_nudge_pending=memory_nudge_pending,
        )

    current_user = get_current_user_message(ledger)
    current_tokens = count_message_tokens(current_user) if current_user else 0
    mixed_signal = compute_mixed_signal(
        state,
        ledger,
        head_tokens=head_tokens,
        reserved_tokens=reserved_tokens,
    )

    middle_raw, tail_raw = split_tail_window(ledger, tail_budget)

    # Layer A：尾部超大工具结果摘要 + 落盘（几乎有就触发）
    # 只读计量（measure_only）时不触发模型摘要/落盘，避免加载历史时阻塞在 LLM 调用。
    tail, layer_a_usages = apply_layer_a_tail_tool_summary(
        tail_raw, session_id=session_id, measure_only=measure_only
    )
    compression_api_usages.extend(layer_a_usages)
    compression_layer = int(CompressionLayer.TAIL_TOOL_SUMMARY) if layer_a_usages else None

    # Layer B：20k 以外旧工具一行摘要 + 去重
    middle = apply_layer_b_old_tool_oneline(middle_raw) if middle_raw else []
    if middle_raw:
        compression_layer = int(CompressionLayer.OLD_TOOL_ONELINE)

    working_body = middle + tail
    body_tokens = count_messages_tokens(working_body)
    tool_tokens = _count_tool_tokens(working_body)
    non_tool_body = body_tokens - tool_tokens

    prelim_head_body = head_tokens + body_tokens + reserved_tokens
    working_total = prelim_head_body

    new_summary = existing_summary
    new_compression_count = compression_count
    compressing = False
    rejected = False
    reject_message = None

    should_compress = (
        mixed_signal >= threshold or working_total > threshold
    ) and not measure_only

    if should_compress:
        compressing = True

        def _compress_fallback() -> tuple:
            logger.warning("上下文压缩失败，保留尾部窗口")
            return sanitize_working_messages(head + working_body), existing_summary, compression_count, None, []

        compressed, new_summary, new_compression_count, comp_result, comp_usages = guarded_step(
            "context.compress",
            lambda: compress_working_messages(
                ledger,
                head_messages=head,
                session_id=session_id,
                max_tokens=max_tokens,
                threshold_ratio=threshold_ratio,
                compression_count=compression_count,
                existing_summary=existing_summary,
            ),
            _compress_fallback,
            session_id=session_id,
            policy=context_step_policy("compress", fallback=_compress_fallback),
        )
        compression_api_usages.extend(comp_usages)

        if comp_result and not comp_result.success:
            rejected = True
            reject_message = comp_result.message
            breakdown = ContextBreakdown(
                bootstrap=bootstrap_tokens,
                skills=skills_tokens,
                summary=estimate_tokens(new_summary),
                recent_turns=body_tokens,
                tool_results=tool_tokens,
                current_message=current_tokens,
                reserved=reserved_tokens,
            )
            local_estimate = breakdown.total
            effective = resolve_effective_tokens(
                state,
                ledger,
                head_tokens=head_tokens,
                reserved_tokens=reserved_tokens,
                local_estimate=local_estimate,
            )
            usage = build_usage_report(
                breakdown,
                max_tokens,
                compression_layer=int(CompressionLayer.REJECT),
                warning=reject_message,
                effective_tokens=effective,
            )
            return AssembleResult(
                messages=[],
                usage=usage,
                context_summary=existing_summary,
                compression_count=new_compression_count,
                rejected=True,
                reject_message=reject_message,
                memory_turn_counter=memory_turn_counter,
                memory_nudge_pending=memory_nudge_pending,
            )

        working_messages = compressed
        if comp_result:
            compression_layer = comp_result.layer
        body_messages = [
            m
            for m in working_messages
            if not isinstance(m, SystemMessage)
            and not (
                isinstance(m, HumanMessage)
                and (m.additional_kwargs or {}).get("context_reference")
            )
            and not (
                isinstance(m, HumanMessage) and (m.additional_kwargs or {}).get("context_summary")
            )
        ]
        body_tokens = count_messages_tokens(body_messages)
        tool_tokens = _count_tool_tokens(body_messages)
        non_tool_body = body_tokens - tool_tokens
    else:
        working_messages = sanitize_working_messages(head + working_body)

    summary_block_tokens = estimate_tokens(new_summary) if new_summary else 0

    breakdown = ContextBreakdown(
        bootstrap=bootstrap_tokens,
        skills=skills_tokens,
        summary=summary_block_tokens,
        recent_turns=non_tool_body,
        tool_results=tool_tokens,
        current_message=current_tokens,
        reserved=reserved_tokens,
    )
    local_estimate = breakdown.total
    effective = resolve_effective_tokens(
        state,
        ledger,
        head_tokens=head_tokens,
        reserved_tokens=reserved_tokens,
        local_estimate=local_estimate,
    )
    warning = None
    usage_ratio = effective / max_tokens if max_tokens else 0
    if measure_only and (mixed_signal >= threshold or working_total > threshold):
        warning = "上下文将超过阈值，下次发送时将自动压缩"
    elif usage_ratio > 0.8:
        warning = "上下文使用率较高，建议新建会话"
    elif usage_ratio > 0.6:
        warning = "上下文使用率上升中"

    usage = build_usage_report(
        breakdown,
        max_tokens,
        compression_layer=compression_layer,
        warning=warning,
        effective_tokens=effective,
    )

    return AssembleResult(
        messages=working_messages,
        usage=usage,
        context_summary=new_summary,
        compression_count=new_compression_count,
        compressing=compressing,
        compression_layer=compression_layer,
        compression_api_usages=compression_api_usages,
        memory_turn_counter=memory_turn_counter,
        memory_nudge_pending=memory_nudge_pending,
    )


def resolve_context_usage(state: dict, session_id: str | None = None) -> dict:
    """基于当前账本实时计算上下文窗口累计占用（只读，不触发压缩或记忆副作用）。"""
    ledger = repair_orphan_tool_calls(get_ledger_messages(state))
    if has_pending_tool_calls(ledger):
        cached = state.get("context_usage") or {}
        if cached:
            return {**cached, "compressing": False}
    assembled = assemble_context(state, session_id, measure_only=True)
    usage = assembled.usage.to_dict()
    usage["compressing"] = False
    if assembled.compression_layer is not None:
        usage["compression_layer"] = assembled.compression_layer
    return usage
