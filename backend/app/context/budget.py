from __future__ import annotations

import json
import math
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from app.context.schemas import ContextBreakdown, ContextUsage
from app.core.settings import get_settings

# 与 LangChain approximate 对齐；中文按 1 字 ≈ 1 token，拉丁文约 4 字符 / token（Qwen 短期估算）
CHARS_PER_TOKEN = 4.0
EXTRA_TOKENS_PER_MESSAGE = 4.0

DEFAULT_CONTEXT_CONFIG = {
    "max_tokens": 128_000,
    "compress_threshold_ratio": 0.75,
    "bootstrap": {
        "single_file_max_chars": 20_000,
        "total_max_chars": 150_000,
    },
    "tool_result_max_tokens": 4_000,
    "tool_summary_max_chars": 500,
    "budget": {
        "bootstrap": 0.18,
        "skills": 0.04,
        "summary": 0.12,
        "recent_turns": 0.28,
        "tool_results": 0.25,
        "current_message": 0.05,
        "reserved": 0.08,
    },
    "recent_turn_pairs": 5,
    "recent_tail_max_tokens": 20_000,
    "summary_max_chars": 3_000,
    "summary_chunk_max_chars": 12_000,
    "max_compression_attempts": 3,
    "memory": {
        "daily_sediment_every_turns": 5,
        "longterm_consolidate_interval_hours": 24,
        "longterm_consolidate_days": 7,
        "hot": {
            "memory_char_limit": 3000,
            "user_char_limit": 1500,
            "section_char_limit": 800,
            "consolidate_interval_hours": 24,
            "max_candidates_per_run": 20,
            "arbiter_batch_by_section": True,
            "empty_section_placeholder": "（空）",
            "misc_compact_every": 5,
        },
        "warm": {
            "nudge_every_user_turns": 5,
            "daily_file_max_chars": 8000,
        },
    },
}


def load_context_config(workspace: Path | None = None) -> dict:
    workspace = workspace or get_settings().workspace_path
    try:
        from app.tools.registry import load_full_config

        full = load_full_config(workspace)
    except Exception:
        return dict(DEFAULT_CONTEXT_CONFIG)
    ctx = full.get("context", {})
    if not isinstance(ctx, dict):
        return dict(DEFAULT_CONTEXT_CONFIG)
    merged = dict(DEFAULT_CONTEXT_CONFIG)
    merged.update({k: v for k, v in ctx.items() if k != "budget" and k != "bootstrap" and k != "memory"})
    if "budget" in ctx and isinstance(ctx["budget"], dict):
        merged["budget"] = {**DEFAULT_CONTEXT_CONFIG["budget"], **ctx["budget"]}
    if "bootstrap" in ctx and isinstance(ctx["bootstrap"], dict):
        merged["bootstrap"] = {**DEFAULT_CONTEXT_CONFIG["bootstrap"], **ctx["bootstrap"]}
    if "memory" in ctx and isinstance(ctx["memory"], dict):
        mem = {**DEFAULT_CONTEXT_CONFIG["memory"], **ctx["memory"]}
        if "hot" in ctx["memory"] and isinstance(ctx["memory"]["hot"], dict):
            mem["hot"] = {**DEFAULT_CONTEXT_CONFIG["memory"]["hot"], **ctx["memory"]["hot"]}
        if "warm" in ctx["memory"] and isinstance(ctx["memory"]["warm"], dict):
            mem["warm"] = {**DEFAULT_CONTEXT_CONFIG["memory"]["warm"], **ctx["memory"]["warm"]}
        merged["memory"] = mem
    return merged


def estimate_tokens(text: str) -> int:
    """纯文本 token 估算：CJK 1:1，其余字符按 CHARS_PER_TOKEN。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return max(1, cjk + math.ceil(other / CHARS_PER_TOKEN))


def message_content_text(msg: BaseMessage) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text", "") or str(part))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content or "")


def count_message_tokens(msg: BaseMessage) -> int:
    return count_tokens_approximately(
        [msg],
        chars_per_token=CHARS_PER_TOKEN,
        extra_tokens_per_message=EXTRA_TOKENS_PER_MESSAGE,
    )


def count_messages_tokens(messages: list[BaseMessage]) -> int:
    if not messages:
        return 0
    return count_tokens_approximately(
        messages,
        chars_per_token=CHARS_PER_TOKEN,
        extra_tokens_per_message=EXTRA_TOKENS_PER_MESSAGE,
    )


def budget_allocation(max_tokens: int, config: dict | None = None) -> dict[str, int]:
    config = config or load_context_config()
    ratios = config.get("budget", DEFAULT_CONTEXT_CONFIG["budget"])
    return {key: int(max_tokens * ratio) for key, ratio in ratios.items()}


def resolve_effective_tokens(
    state: dict,
    ledger: list[BaseMessage],
    *,
    head_tokens: int = 0,
    reserved_tokens: int = 0,
    local_estimate: int,
) -> int:
    """有效用量：max(API 实测+账本增量, 本地粗估)。"""
    mixed = compute_mixed_signal(
        state,
        ledger,
        head_tokens=head_tokens,
        reserved_tokens=reserved_tokens,
    )
    return max(mixed, local_estimate)


def build_usage_report(
    breakdown: ContextBreakdown,
    max_tokens: int,
    *,
    compression_layer: int | None = None,
    warning: str | None = None,
    effective_tokens: int | None = None,
) -> ContextUsage:
    local_estimate = breakdown.total
    display = effective_tokens if effective_tokens is not None else local_estimate
    ratio = display / max_tokens if max_tokens else 0.0
    return ContextUsage(
        total_tokens=display,
        max_tokens=max_tokens,
        ratio=ratio,
        breakdown=breakdown,
        compression_layer=compression_layer,
        warning=warning,
        estimated_tokens=local_estimate if local_estimate != display else None,
    )


def compute_mixed_signal(
    state: dict,
    ledger: list[BaseMessage],
    *,
    head_tokens: int = 0,
    reserved_tokens: int = 0,
) -> int:
    """估算下一次 LLM 调用的 prompt 规模。

    公式：上一轮最后一次 API 实测 (prompt + completion + reasoning)
          + 自该次调用以来账本新增消息的本地 token 估算。

    这样可覆盖同一用户轮内工具执行结果、中间 AI 输出等未出现在
    上次 prompt_tokens 中的增量；首轮无 API 记录时回退为本地全量估算。
    """
    api_usage = state.get("api_usage") or {}
    last_prompt = int(api_usage.get("prompt_tokens") or 0)
    last_completion = int(api_usage.get("completion_tokens") or 0)
    reasoning = int(api_usage.get("reasoning_tokens") or 0)

    last_len = int(state.get("last_invoke_ledger_len") or 0)
    if last_len < 0 or last_len > len(ledger):
        last_len = 0
    delta_tokens = count_messages_tokens(ledger[last_len:])

    if last_prompt > 0:
        return last_prompt + last_completion + reasoning + delta_tokens

    return head_tokens + count_messages_tokens(ledger) + reserved_tokens


def threshold_tokens(max_tokens: int, threshold_ratio: float) -> int:
    return int(max_tokens * threshold_ratio)


def classify_messages(messages: list[BaseMessage]) -> dict[str, list[BaseMessage]]:
    system: list[BaseMessage] = []
    human: list[BaseMessage] = []
    ai: list[BaseMessage] = []
    tools: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            system.append(msg)
        elif isinstance(msg, HumanMessage):
            human.append(msg)
        elif isinstance(msg, AIMessage):
            ai.append(msg)
        elif isinstance(msg, ToolMessage):
            tools.append(msg)
    return {"system": system, "human": human, "ai": ai, "tools": tools}
