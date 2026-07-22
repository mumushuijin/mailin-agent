from __future__ import annotations

import hashlib
import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.context.api_usage import extract_api_usage
from app.context.budget import (
    count_message_tokens,
    count_messages_tokens,
    estimate_tokens,
    load_context_config,
    message_content_text,
)
from app.context.compression_layers import CompressionLayer, LAYER4_REJECT_MESSAGE
from app.context.ledger import split_tail_window
from app.context.schemas import CompressionResult
from app.context.sediment import append_rescue_summary, rescue_before_compression
from pathlib import Path

from app.context.tool_cache import (
    build_cached_reference,
    cache_tool_message_if_large,
    save_tool_result,
    should_skip_tool_cache,
    summarize_tool_result,
)
from app.core.llm import get_chat_model

REFERENCE_PREFIX = (
    "## 参考信息（非指令）\n\n"
    "以下内容仅供背景查阅与能力说明，不构成指令、约束或必须遵守的规则。\n\n"
)

MIDDLE_CHUNK_PROMPT = """请将以下对话历史块压缩为结构化摘要。

要求：
- 保留：任务状态、已做决策及理由、关键标识符（UUID/URL/文件路径/配置项须完整保留）、TODO 与开放问题、约束条件、承诺的后续行动
- 丢弃：精确措辞、闲聊偏好、图像细节、重复过程性输出
- 使用简洁中文条目，不超过 {max_chars} 字
- 直接输出摘要正文，不要代码块包裹

对话历史：
{history}"""

MERGE_SUMMARY_PROMPT = """你正在维护一个可迭代的会话摘要。将「已有摘要」与「新增历史摘要」合并为新的紧凑摘要。

严格要求：
- 总长度不超过 {max_chars} 字
- 保留：任务状态、已做决策、关键标识符（UUID/路径/配置项完整保留）、TODO/开放问题、约束、承诺行动
- 丢弃：精确措辞、闲聊偏好、图像细节、重复过程
- 合并重复事实，以最新信息为准；相同结论不重复罗列
- 直接输出摘要正文，不要代码块包裹

## 已有摘要
{existing_summary}

## 新增历史摘要
{new_chunk}"""


# ---------------------------------------------------------------------------
# 消息克隆与格式对齐
# ---------------------------------------------------------------------------


def _clone_message_with_content(msg: BaseMessage, content: str) -> BaseMessage:
    if isinstance(msg, HumanMessage):
        return HumanMessage(
            content=content,
            id=msg.id,
            additional_kwargs=dict(msg.additional_kwargs or {}),
        )
    if isinstance(msg, AIMessage):
        return AIMessage(
            content=content,
            id=msg.id,
            tool_calls=msg.tool_calls,
            additional_kwargs=dict(msg.additional_kwargs or {}),
        )
    if isinstance(msg, ToolMessage):
        return ToolMessage(
            content=content,
            tool_call_id=msg.tool_call_id,
            id=msg.id,
            name=getattr(msg, "name", None),
            additional_kwargs=dict(msg.additional_kwargs or {}),
        )
    if isinstance(msg, SystemMessage):
        return SystemMessage(content=content, id=msg.id)
    return msg


def _strip_ai_tool_calls(ai: AIMessage, content: str | None = None) -> AIMessage:
    extra = {
        k: v
        for k, v in (ai.additional_kwargs or {}).items()
        if k not in ("tool_calls", "function_call")
    }
    return AIMessage(
        content=content if content is not None else ai.content,
        id=ai.id,
        tool_calls=[],
        additional_kwargs=extra,
    )


def _messages_to_text(messages: list[BaseMessage]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = msg.type
        text = message_content_text(msg)
        if isinstance(msg, AIMessage) and msg.tool_calls:
            text += "\n[tool_calls] " + json.dumps(msg.tool_calls, ensure_ascii=False)
        parts.append(f"{role}: {text}")
    return "\n\n".join(parts)


def build_reference_block(
    *,
    skills_catalog: str = "",
    memory_hints: str = "",
    tools_disclosure: str = "",
    context_summary: str = "",
) -> HumanMessage | None:
    """参考信息块：非 SystemMessage，明确标注为非指令。"""
    parts: list[str] = []
    if skills_catalog.strip():
        parts.append(skills_catalog.strip())
    if memory_hints.strip():
        parts.append(memory_hints.strip())
    if tools_disclosure.strip():
        parts.append(tools_disclosure.strip())
    if context_summary.strip():
        parts.append(f"## 会话摘要\n\n{context_summary.strip()}")
    if not parts:
        return None
    content = REFERENCE_PREFIX + "\n\n---\n\n".join(parts)
    return HumanMessage(content=content, additional_kwargs={"context_reference": True})


def build_summary_message(summary: str) -> HumanMessage:
    return HumanMessage(
        content=f"## 会话摘要\n\n{summary.strip()}",
        additional_kwargs={"context_summary": True},
    )


def sanitize_working_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """确保 ToolMessage 前必有带 tool_calls 的 AIMessage，否则折叠为普通 assistant 文本。"""
    result: list[BaseMessage] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if isinstance(msg, AIMessage) and msg.tool_calls:
            chain_ai = msg
            tool_messages: list[ToolMessage] = []
            expected_ids = [tc.get("id") for tc in chain_ai.tool_calls if tc.get("id")]
            j = i + 1
            while j < len(messages) and isinstance(messages[j], ToolMessage):
                tm = messages[j]
                if not expected_ids or tm.tool_call_id in expected_ids:
                    tool_messages.append(tm)
                j += 1
            if tool_messages and len(tool_messages) >= len(expected_ids):
                result.append(chain_ai)
                for tm in tool_messages[: len(expected_ids) or len(tool_messages)]:
                    result.append(tm)
            elif tool_messages:
                merged = _tool_chain_fallback_text(chain_ai, tool_messages)
                result.append(_strip_ai_tool_calls(chain_ai, merged))
            else:
                result.append(chain_ai)
            i = j
            continue
        if isinstance(msg, ToolMessage):
            text = message_content_text(msg)
            name = getattr(msg, "name", None) or "tool"
            result.append(AIMessage(content=f"[历史工具结果] {name}: {text[:500]}"))
            i += 1
            continue
        result.append(msg)
        i += 1
    return result


def _tool_chain_fallback_text(ai: AIMessage, tool_messages: list[ToolMessage]) -> str:
    parts = [f"调用了 {tc.get('name', 'tool')}" for tc in ai.tool_calls]
    header = f"[历史工具调用已降级] {'; '.join(parts)}"
    summaries = [
        f"{getattr(tm, 'name', None) or 'tool'}: {message_content_text(tm)[:200]}"
        for tm in tool_messages
    ]
    if summaries:
        return header + "\n" + "\n".join(summaries)
    return header


def _find_ai_for_tool(messages: list[BaseMessage], tm: ToolMessage) -> AIMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            ids = {tc.get("id") for tc in msg.tool_calls}
            if tm.tool_call_id in ids:
                return msg
    return None


def _content_fingerprint(content: str) -> str:
    normalized = content.strip().replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Layer A — 尾部窗口内超大工具结果：模型摘要 + 落盘
# ---------------------------------------------------------------------------


def _summarize_tail_tool_with_model(content: str, max_chars: int) -> tuple[str, list[dict]]:
    api_usages: list[dict] = []
    try:
        model = get_chat_model()
        prompt = (
            f"用简洁中文概括以下工具输出要点（{max_chars} 字以内），"
            "保留关键结论、数值、路径与错误信息：\n\n"
            f"{content[:16000]}"
        )
        resp = model.invoke([HumanMessage(content=prompt)])
        usage = extract_api_usage(resp, source="compression_tail_tool")
        if usage:
            api_usages.append(usage)
        if resp.content:
            return str(resp.content).strip(), api_usages
    except Exception:
        pass
    return summarize_tool_result(content, max_chars=max_chars), api_usages


def apply_layer_a_tail_tool_summary(
    messages: list[BaseMessage],
    *,
    session_id: str | None = None,
    measure_only: bool = False,
) -> tuple[list[BaseMessage], list[dict]]:
    """Layer A：尾部窗口内超长工具结果 → 模型摘要（首选）+ 路径落盘。

    measure_only=True 时为只读计量：不触发模型摘要、不落盘，改用本地廉价摘要
    估算 token，避免加载会话历史时阻塞在 LLM 调用上。
    """
    if not messages:
        return messages, []

    config = load_context_config()
    max_tokens = int(config.get("tool_result_max_tokens", 4000))
    max_chars = int(config.get("tool_summary_max_chars", 500))
    api_usages: list[dict] = []
    result: list[BaseMessage] = []

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            result.append(msg)
            continue

        # 已标记跳过缓存的工具结果（如读取 tool_results/ 落盘文件的回取结果）：
        # 绝不再次落盘/摘要，否则会形成“读大文件 → 落更大文件 → 再读”的套娃。
        # read_file 对落盘文件已强制 token 上限，这里只需原样放行。
        if should_skip_tool_cache(msg):
            result.append(msg)
            continue

        kwargs = dict(msg.additional_kwargs or {})
        cache_path = kwargs.get("tool_cache_path")
        cache_summary = kwargs.get("tool_cache_summary")
        if cache_path and cache_summary:
            result.append(
                _clone_message_with_content(
                    msg,
                    build_cached_reference(Path(str(cache_path)), str(cache_summary)),
                )
            )
            continue

        content = message_content_text(msg)
        if estimate_tokens(content) <= max_tokens:
            result.append(msg)
            continue

        # 只读计量：不调用模型、不落盘，用本地廉价摘要估算 token
        if measure_only:
            result.append(
                _clone_message_with_content(
                    msg, f"[工具结果已摘要] {summarize_tool_result(content, max_chars=max_chars)}"
                )
            )
            continue

        if session_id:
            cached = cache_tool_message_if_large(msg, session_id)
            if cached is not msg:
                kwargs = dict(cached.additional_kwargs or {})
                cache_path = kwargs.get("tool_cache_path")
                cache_summary = kwargs.get("tool_cache_summary")
                if cache_path and cache_summary:
                    result.append(
                        _clone_message_with_content(
                            cached,
                            build_cached_reference(Path(str(cache_path)), str(cache_summary)),
                        )
                    )
                    continue

        summary, usages = _summarize_tail_tool_with_model(content, max_chars)
        api_usages.extend(usages)
        if session_id:
            path = save_tool_result(session_id, msg.tool_call_id or "unknown", content)
            working = build_cached_reference(path, summary)
        else:
            working = f"[工具结果已摘要] {summary}"
        result.append(_clone_message_with_content(msg, working))

    return result, api_usages


# ---------------------------------------------------------------------------
# Layer B — 20k 以外旧工具结果：一行可读摘要 + 去重
# ---------------------------------------------------------------------------


def _oneline_tool_summary(tm: ToolMessage, messages: list[BaseMessage]) -> str:
    tool_name = getattr(tm, "name", None) or "tool"
    content = message_content_text(tm)
    char_count = len(content)
    line_count = max(1, content.count("\n") + 1)

    action = f"调用工具 {tool_name}"
    ai = _find_ai_for_tool(messages, tm)
    if ai and ai.tool_calls:
        for tc in ai.tool_calls:
            if tc.get("id") != tm.tool_call_id:
                continue
            args = tc.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                break
            if tool_name in ("read_file", "write_file", "edit_file") and args.get("file_path"):
                verb = "读取文件" if tool_name == "read_file" else "写入/编辑文件"
                action = f"{verb} {args['file_path']}"
            elif args.get("command"):
                action = f"执行命令 {args['command']}"
            elif args.get("file_path"):
                action = f"操作文件 {args['file_path']}"
            break

    kwargs = tm.additional_kwargs or {}
    path = kwargs.get("tool_cache_path", "")
    parts = [f"[历史工具结果] {action}；输出约 {char_count} 字符/{line_count} 行"]
    if path:
        parts.append(f"已落盘: {path}")
    return "；".join(parts)


def apply_layer_b_old_tool_oneline(
    messages: list[BaseMessage],
) -> list[BaseMessage]:
    """Layer B：中间区域旧工具结果替换为一行可读摘要，并去重。"""
    if not messages:
        return messages

    seen: dict[str, str] = {}
    result: list[BaseMessage] = []

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            result.append(msg)
            continue

        content = message_content_text(msg)
        fingerprint = _content_fingerprint(content)
        if fingerprint in seen:
            result.append(
                _clone_message_with_content(
                    msg,
                    f"[重复工具结果] 与先前记录相同（指纹 {fingerprint}），详见: {seen[fingerprint]}",
                )
            )
            continue

        oneline = _oneline_tool_summary(msg, messages)
        seen[fingerprint] = oneline
        result.append(_clone_message_with_content(msg, oneline))

    return result


# ---------------------------------------------------------------------------
# Layer C — 中间历史分块结构化摘要
# ---------------------------------------------------------------------------


def _chunk_messages_by_chars(messages: list[BaseMessage], max_chars: int) -> list[list[BaseMessage]]:
    chunks: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []
    current_chars = 0
    for msg in messages:
        text = _messages_to_text([msg])
        if current and current_chars + len(text) > max_chars:
            chunks.append(current)
            current = [msg]
            current_chars = len(text)
        else:
            current.append(msg)
            current_chars += len(text)
    if current:
        chunks.append(current)
    return chunks


def _summarize_chunk(history_text: str, max_chars: int) -> tuple[str, list[dict]]:
    api_usages: list[dict] = []
    if not history_text.strip():
        return "", api_usages
    try:
        model = get_chat_model()
        prompt = MIDDLE_CHUNK_PROMPT.format(max_chars=max_chars, history=history_text[:max_chars * 4])
        resp = model.invoke([HumanMessage(content=prompt)])
        usage = extract_api_usage(resp, source="compression_middle_chunk")
        if usage:
            api_usages.append(usage)
        if resp.content:
            return str(resp.content).strip(), api_usages
    except Exception:
        api_usages.extend(rescue_before_compression(history_text))
    return history_text[: max_chars - 3] + "...", api_usages


def _merge_summaries(existing_summary: str, new_chunk: str, max_chars: int) -> tuple[str, list[dict]]:
    api_usages: list[dict] = []
    if not existing_summary.strip():
        return new_chunk.strip(), api_usages
    if not new_chunk.strip():
        return existing_summary.strip(), api_usages
    try:
        model = get_chat_model()
        prompt = MERGE_SUMMARY_PROMPT.format(
            max_chars=max_chars,
            existing_summary=existing_summary[: max_chars * 2],
            new_chunk=new_chunk[: max_chars * 2],
        )
        resp = model.invoke([HumanMessage(content=prompt)])
        usage = extract_api_usage(resp, source="compression_merge_summary")
        if usage:
            api_usages.append(usage)
        if resp.content:
            merged = str(resp.content).strip()
            append_rescue_summary(merged)
            return merged, api_usages
    except Exception:
        api_usages.extend(rescue_before_compression(existing_summary + "\n" + new_chunk))
    merged = (existing_summary + "\n\n" + new_chunk).strip()
    if len(merged) > max_chars:
        merged = merged[: max_chars - 3] + "..."
    return merged, api_usages


def apply_layer_c_middle_summary(
    middle_messages: list[BaseMessage],
    existing_summary: str = "",
) -> CompressionResult:
    """Layer C：中间历史分块摘要 → 合并为可迭代 context_summary。"""
    config = load_context_config()
    summary_max = int(config.get("summary_max_chars", 3000))
    chunk_max = int(config.get("summary_chunk_max_chars", 12000))
    per_chunk_budget = max(400, summary_max // max(1, len(middle_messages) // 5 + 1))

    api_usages: list[dict] = []
    if not middle_messages:
        return CompressionResult(
            summary=existing_summary,
            kept_messages=[],
            layer=int(CompressionLayer.MIDDLE_SUMMARY),
            success=True,
            api_usages=api_usages,
        )

    chunks = _chunk_messages_by_chars(middle_messages, chunk_max)
    chunk_summaries: list[str] = []
    for chunk in chunks:
        text = _messages_to_text(chunk)
        summary, usages = _summarize_chunk(text, per_chunk_budget)
        api_usages.extend(usages)
        if summary:
            chunk_summaries.append(summary)

    combined_new = "\n\n".join(chunk_summaries).strip()
    merged, merge_usages = _merge_summaries(existing_summary, combined_new, summary_max)
    api_usages.extend(merge_usages)

    return CompressionResult(
        summary=merged,
        kept_messages=[],
        layer=int(CompressionLayer.MIDDLE_SUMMARY),
        success=True,
        api_usages=api_usages,
    )


def build_layer_d_reject_result(
    working: list[BaseMessage],
    summary: str,
    *,
    message: str | None = None,
) -> CompressionResult:
    return CompressionResult(
        summary=summary,
        kept_messages=working,
        layer=int(CompressionLayer.REJECT),
        success=False,
        message=message or LAYER4_REJECT_MESSAGE,
    )


# ---------------------------------------------------------------------------
# 压缩管线编排
# ---------------------------------------------------------------------------


def compress_working_messages(
    ledger: list[BaseMessage],
    *,
    head_messages: list[BaseMessage],
    session_id: str | None,
    max_tokens: int,
    threshold_ratio: float,
    compression_count: int,
    existing_summary: str = "",
) -> tuple[list[BaseMessage], str, int, CompressionResult | None, list[dict]]:
    """
    分层递进压缩：A（尾部工具）→ B（旧工具一行）→ C（中间摘要）→ D（拒绝）。

    返回 (working_messages, summary, new_compression_count, result, api_usages)。
    """
    config = load_context_config()
    max_attempts = int(config.get("max_compression_attempts", 3))
    threshold = int(max_tokens * threshold_ratio)
    tail_budget = int(config.get("recent_tail_max_tokens", 20_000))

    middle_raw, tail_raw = split_tail_window(ledger, tail_budget)
    api_usages: list[dict] = []

    tail, tail_usages = apply_layer_a_tail_tool_summary(tail_raw, session_id=session_id)
    api_usages.extend(tail_usages)

    middle = apply_layer_b_old_tool_oneline(middle_raw) if middle_raw else []

    working = sanitize_working_messages(head_messages + middle + tail)
    total = count_messages_tokens(working)
    if total <= threshold:
        summary = existing_summary
        return working, summary, 0, None, api_usages

    if compression_count >= max_attempts:
        return (
            working,
            existing_summary,
            compression_count,
            build_layer_d_reject_result(working, existing_summary),
            api_usages,
        )

    layer_c = apply_layer_c_middle_summary(middle, existing_summary)
    api_usages.extend(layer_c.api_usages)
    new_summary = layer_c.summary

    reference = next(
        (m for m in head_messages if isinstance(m, HumanMessage) and (m.additional_kwargs or {}).get("context_reference")),
        None,
    )
    if reference:
        ref_content = message_content_text(reference)
        if "## 会话摘要" in ref_content:
            base, _, _ = ref_content.partition("## 会话摘要")
            ref_content = base.rstrip() + f"\n\n---\n\n## 会话摘要\n\n{new_summary}"
        else:
            ref_content = ref_content.rstrip() + f"\n\n---\n\n## 会话摘要\n\n{new_summary}"
        head = [
            _clone_message_with_content(reference, ref_content) if m is reference else m
            for m in head_messages
        ]
    else:
        head = head_messages + [build_summary_message(new_summary)]

    working = sanitize_working_messages(head + tail)
    total = count_messages_tokens(working)
    if total <= threshold:
        return working, new_summary, 0, layer_c, api_usages

    new_count = compression_count + 1
    if new_count >= max_attempts:
        return (
            working,
            new_summary,
            new_count,
            build_layer_d_reject_result(working, new_summary),
            api_usages,
        )
    return working, new_summary, new_count, layer_c, api_usages


def emergency_compress(
    ledger: list[BaseMessage],
    *,
    head_messages: list[BaseMessage] | None = None,
    session_id: str | None = None,
) -> tuple[list[BaseMessage], list[dict]]:
    """API 上下文超长时的紧急兜底：强摘要 + 仅保留最末关键消息。"""
    tail_budget = int(load_context_config().get("recent_tail_max_tokens", 20_000))
    middle_raw, tail_raw = split_tail_window(ledger, tail_budget)
    api_usages: list[dict] = []

    tail, tail_usages = apply_layer_a_tail_tool_summary(tail_raw, session_id=session_id)
    api_usages.extend(tail_usages)

    all_middle = middle_raw + tail
    layer_c = apply_layer_c_middle_summary(all_middle)
    api_usages.extend(layer_c.api_usages)

    kept: list[BaseMessage] = []
    for msg in reversed(ledger):
        if isinstance(msg, HumanMessage):
            kept.insert(0, msg)
            break
    for msg in reversed(ledger):
        if isinstance(msg, AIMessage) and not msg.tool_calls and message_content_text(msg).strip():
            if msg not in kept:
                kept.insert(0, msg)
            break
    kept = kept[-3:]

    head = list(head_messages or [])
    if head:
        working = sanitize_working_messages(head + [build_summary_message(layer_c.summary)] + kept)
    else:
        working = sanitize_working_messages(
            [SystemMessage(content=f"## 紧急会话摘要\n\n{layer_c.summary}")] + kept
        )
    return working, api_usages
