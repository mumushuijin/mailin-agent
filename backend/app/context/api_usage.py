from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage


def _sum_optional(values: list[int | None]) -> int:
    return sum(v for v in values if isinstance(v, int))


def extract_api_usage(message: BaseMessage, *, source: str = "agent") -> dict[str, Any] | None:
    """从 LangChain 响应消息提取 API 返回的 token 用量（含 DeepSeek 缓存字段）。"""
    if not isinstance(message, AIMessage):
        return None

    usage_meta = message.usage_metadata
    raw_usage = message.response_metadata.get("token_usage")
    if not usage_meta and not raw_usage:
        return None

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    cache_hit: int | None = None
    cache_miss: int | None = None
    reasoning: int | None = None

    if isinstance(usage_meta, dict):
        prompt_tokens = int(usage_meta.get("input_tokens") or 0)
        completion_tokens = int(usage_meta.get("output_tokens") or 0)
        total_tokens = int(usage_meta.get("total_tokens") or prompt_tokens + completion_tokens)
        input_details = usage_meta.get("input_token_details") or {}
        output_details = usage_meta.get("output_token_details") or {}
        if isinstance(input_details, dict):
            cache_hit = input_details.get("cache_read")
        if isinstance(output_details, dict):
            reasoning = output_details.get("reasoning")

    if isinstance(raw_usage, dict):
        prompt_tokens = int(raw_usage.get("prompt_tokens") or prompt_tokens)
        completion_tokens = int(raw_usage.get("completion_tokens") or completion_tokens)
        total_tokens = int(raw_usage.get("total_tokens") or total_tokens or prompt_tokens + completion_tokens)
        cache_hit = raw_usage.get("prompt_cache_hit_tokens", cache_hit)
        cache_miss = raw_usage.get("prompt_cache_miss_tokens", cache_miss)
        details = raw_usage.get("completion_tokens_details") or {}
        if isinstance(details, dict):
            reasoning = details.get("reasoning_tokens", reasoning)

    if total_tokens <= 0 and prompt_tokens <= 0 and completion_tokens <= 0:
        return None

    result: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens or prompt_tokens + completion_tokens,
        "source": source,
    }
    if cache_hit is not None:
        result["prompt_cache_hit_tokens"] = int(cache_hit)
    if cache_miss is not None:
        result["prompt_cache_miss_tokens"] = int(cache_miss)
    if reasoning is not None:
        result["reasoning_tokens"] = int(reasoning)
    return result


def merge_api_usages(*usages: dict[str, Any] | None) -> dict[str, Any] | None:
    """合并同一步骤内多次 LLM 调用的用量。"""
    valid = [u for u in usages if u]
    if not valid:
        return None

    sources = sorted({u.get("source", "unknown") for u in valid})
    merged: dict[str, Any] = {
        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in valid),
        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in valid),
        "total_tokens": sum(int(u.get("total_tokens") or 0) for u in valid),
        "sources": sources,
    }
    cache_hit = _sum_optional([u.get("prompt_cache_hit_tokens") for u in valid])
    cache_miss = _sum_optional([u.get("prompt_cache_miss_tokens") for u in valid])
    reasoning = _sum_optional([u.get("reasoning_tokens") for u in valid])
    if cache_hit:
        merged["prompt_cache_hit_tokens"] = cache_hit
    if cache_miss:
        merged["prompt_cache_miss_tokens"] = cache_miss
    if reasoning:
        merged["reasoning_tokens"] = reasoning
    return merged


def merge_session_token_stats(
    existing: dict[str, Any] | None,
    step_usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """累计会话级 API token 统计。"""
    stats = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "reasoning_tokens": 0,
        "request_count": 0,
    }
    if existing:
        for key in stats:
            stats[key] = int(existing.get(key) or 0)

    if not step_usage:
        return stats

    stats["prompt_tokens"] += int(step_usage.get("prompt_tokens") or 0)
    stats["completion_tokens"] += int(step_usage.get("completion_tokens") or 0)
    stats["total_tokens"] += int(step_usage.get("total_tokens") or 0)
    stats["prompt_cache_hit_tokens"] += int(step_usage.get("prompt_cache_hit_tokens") or 0)
    stats["reasoning_tokens"] += int(step_usage.get("reasoning_tokens") or 0)
    stats["request_count"] += 1
    return stats


def build_step_api_usage(
    *,
    agent_usage: dict[str, Any] | None,
    compression_usages: list[dict[str, Any]] | None,
    estimated_prompt_tokens: int | None = None,
) -> dict[str, Any] | None:
    """构建单步 API 用量报告，附带本地估算供校准对比。"""
    compression_merged = merge_api_usages(*(compression_usages or []))
    step_total = merge_api_usages(agent_usage, compression_merged)
    if not step_total:
        return None

    result = dict(step_total)
    if agent_usage:
        result["agent"] = agent_usage
    if compression_merged:
        result["compression"] = compression_merged
    if estimated_prompt_tokens is not None:
        result["estimated_prompt_tokens"] = estimated_prompt_tokens
        actual_prompt = int(step_total.get("prompt_tokens") or 0)
        if actual_prompt > 0:
            result["estimate_delta"] = estimated_prompt_tokens - actual_prompt
    return result
