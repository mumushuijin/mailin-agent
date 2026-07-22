from __future__ import annotations

import json
import re
from pathlib import Path

from langchain_core.messages import BaseMessage, ToolMessage

from app.context.budget import estimate_tokens, load_context_config, message_content_text
from app.context.ledger import is_current_user_turn
from app.core.settings import get_settings

CACHED_PREFIX = "[工具结果已落盘]"
SKIP_CACHE_KWARG = "skip_tool_cache"


def is_tool_results_path(file_path: str) -> bool:
    """路径是否位于 tool_results 缓存目录下。"""
    normalized = file_path.replace("\\", "/").strip().lstrip("/")
    return normalized.startswith("tool_results/")


def tool_results_dir(session_id: str, workspace: Path | None = None) -> Path:
    workspace = workspace or get_settings().workspace_path
    path = workspace / "tool_results" / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def summarize_tool_result(content: str, max_chars: int | None = None) -> str:
    config = load_context_config()
    max_chars = max_chars or config.get("tool_summary_max_chars", 500)
    text = content.strip().replace("\r\n", "\n")
    if len(text) <= max_chars:
        return text
    lines = text.split("\n")
    head = "\n".join(lines[:8])
    if len(head) > max_chars:
        return head[: max_chars - 3] + "..."
    return head + f"\n...（共 {len(text)} 字符，已截断）"


def save_tool_result(session_id: str, tool_call_id: str, content: str, workspace: Path | None = None) -> Path:
    directory = tool_results_dir(session_id, workspace)
    safe_id = re.sub(r"[^\w\-]", "_", tool_call_id or "unknown")
    path = directory / f"{safe_id}.json"
    payload = {"tool_call_id": tool_call_id, "content": content}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_cached_reference(path: Path, summary: str) -> str:
    rel = path.as_posix()
    return f"{CACHED_PREFIX} 路径: {rel} | 摘要: {summary}"


def should_skip_tool_cache(msg: ToolMessage) -> bool:
    kwargs = msg.additional_kwargs or {}
    if kwargs.get(SKIP_CACHE_KWARG):
        return True
    return False


def cache_tool_message_if_large(
    msg: ToolMessage,
    session_id: str,
    workspace: Path | None = None,
) -> ToolMessage:
    """对超大工具结果落盘，在 additional_kwargs 中记录缓存信息（账本仍保留全文）。"""
    kwargs = dict(msg.additional_kwargs or {})
    if should_skip_tool_cache(msg):
        return msg
    if kwargs.get("tool_cache_path"):
        return msg

    config = load_context_config(workspace)
    max_tokens = config.get("tool_result_max_tokens", 4000)
    content = message_content_text(msg)
    if estimate_tokens(content) <= max_tokens:
        return msg

    path = save_tool_result(session_id, msg.tool_call_id or "unknown", content, workspace)
    summary = summarize_tool_result(content)
    kwargs["tool_cache_path"] = str(path)
    kwargs["tool_cache_summary"] = summary
    return ToolMessage(
        content=content,
        tool_call_id=msg.tool_call_id,
        id=msg.id,
        name=getattr(msg, "name", None),
        additional_kwargs=kwargs,
    )


def merge_tool_cache_updates(
    messages: list[BaseMessage],
    updates: list[ToolMessage],
) -> list[BaseMessage]:
    """将落盘元数据合并回账本视图（同 id 覆盖 additional_kwargs）。"""
    if not updates:
        return messages
    by_id = {u.id: u for u in updates if u.id}
    merged: list[BaseMessage] = []
    for msg in messages:
        if msg.id and msg.id in by_id:
            merged.append(by_id[msg.id])
        else:
            merged.append(msg)
    return merged


def defer_cache_tool_messages(
    messages: list[BaseMessage],
    session_id: str,
    workspace: Path | None = None,
) -> list[ToolMessage]:
    """轮次边界：对非当前轮、未落盘的超大工具结果落盘元数据到账本。"""
    updates: list[ToolMessage] = []
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if is_current_user_turn(msg, messages):
            continue
        cached = cache_tool_message_if_large(msg, session_id, workspace)
        if cached is not msg:
            updates.append(cached)
    return updates


def process_tool_message_for_cache(
    msg: ToolMessage,
    session_id: str,
    workspace: Path | None = None,
) -> ToolMessage:
    """工具执行后：本轮不落盘，仅保留全文（兼容旧调用点）。"""
    return msg


def working_content_for_tool_message(
    msg: ToolMessage,
    messages: list[BaseMessage] | None = None,
) -> str:
    """工作视图中工具结果的展示文本（已落盘则引用，否则按大小截断）。"""
    kwargs = msg.additional_kwargs or {}
    cache_path = kwargs.get("tool_cache_path")
    cache_summary = kwargs.get("tool_cache_summary")
    if cache_path and cache_summary:
        return build_cached_reference(Path(str(cache_path)), str(cache_summary))

    content = message_content_text(msg)
    config = load_context_config()
    max_tokens = config.get("tool_result_max_tokens", 4000)
    if estimate_tokens(content) > max_tokens:
        return f"{CACHED_PREFIX} 摘要: {summarize_tool_result(content)}"
    return content
