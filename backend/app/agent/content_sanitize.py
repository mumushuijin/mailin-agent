from __future__ import annotations

import json
import re
import uuid
from typing import Any

from langchain_core.messages import AIMessage

# DeepSeek 等模型偶发把工具调用泄漏为正文 DSML（全角竖线 ｜）
_DSML_MARKER = "\uFF5C\uFF5CDSML\uFF5C\uFF5C"

_INVOKE_RE = re.compile(
    rf"<{_DSML_MARKER}invoke\s+name=\"([^\"]+)\"\s*>(.*?)</{_DSML_MARKER}invoke>",
    re.DOTALL,
)

_PARAM_RE = re.compile(
    rf"<{_DSML_MARKER}parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(?:true|false)\")?\s*>"
    rf"(.*?)</{_DSML_MARKER}parameter>",
    re.DOTALL,
)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content or "")


def _parse_parameter_value(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return text
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if text.isdigit():
        return int(text)
    try:
        return float(text)
    except ValueError:
        return text


def parse_dsml_tool_calls(text: str) -> list[dict[str, Any]]:
    """从 DSML 正文提取工具调用，转为 LangChain tool_calls 结构。"""
    if not text or "DSML" not in text:
        return []

    calls: list[dict[str, Any]] = []
    for invoke_name, body in _INVOKE_RE.findall(text):
        args: dict[str, Any] = {}
        for param_name, param_raw in _PARAM_RE.findall(body):
            args[param_name] = _parse_parameter_value(param_raw)

        if invoke_name in {"tool_call", "tool_search", "tool_describe"}:
            if invoke_name == "tool_call" and "arguments" in args and isinstance(args["arguments"], str):
                try:
                    args["arguments"] = json.loads(args["arguments"])
                except json.JSONDecodeError:
                    pass
            calls.append(
                {
                    "name": invoke_name,
                    "args": args,
                    "id": f"dsml_{uuid.uuid4().hex[:12]}",
                }
            )
        else:
            # 直接调用底层工具名（未走 tool_call 代理）
            calls.append(
                {
                    "name": "tool_call",
                    "args": {"name": invoke_name, "arguments": args},
                    "id": f"dsml_{uuid.uuid4().hex[:12]}",
                }
            )
    return calls


def strip_dsml_markup(text: str) -> str:
    """移除响应中的 DSML 工具调用标记，避免直接展示给用户。"""
    if not text or "DSML" not in text:
        return text
    cleaned = re.sub(
        rf"<{_DSML_MARKER}tool_calls>.*?</{_DSML_MARKER}tool_calls>",
        "",
        text,
        flags=re.DOTALL,
    )
    cleaned = re.sub(rf"</?{_DSML_MARKER}[^>]*>", "", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _clean_message_content(content: Any) -> Any:
    if isinstance(content, str):
        return strip_dsml_markup(content)
    if isinstance(content, list):
        cleaned_blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = strip_dsml_markup(str(block.get("text") or ""))
                if text:
                    cleaned_blocks.append({"type": "text", "text": text})
            elif isinstance(block, dict) and block.get("type") == "tool_call":
                cleaned_blocks.append(block)
        if cleaned_blocks:
            return cleaned_blocks
        text = strip_dsml_markup(_content_to_text(content))
        return text
    return content


def normalize_ai_message(msg: AIMessage) -> AIMessage:
    """剥离 DSML 泄漏文本，并尽量恢复为正式 tool_calls 供图路由到 tools 节点。"""
    text = _content_to_text(msg.content)
    if "DSML" not in text and not msg.tool_calls:
        return msg

    recovered = parse_dsml_tool_calls(text)
    cleaned_content = _clean_message_content(msg.content)

    tool_calls = list(msg.tool_calls or [])
    if recovered:
        if not tool_calls:
            tool_calls = recovered
        else:
            seen = {
                (tc.get("name"), json.dumps(tc.get("args"), sort_keys=True, default=str))
                for tc in tool_calls
            }
            for tc in recovered:
                key = (tc.get("name"), json.dumps(tc.get("args"), sort_keys=True, default=str))
                if key not in seen:
                    tool_calls.append(tc)
                    seen.add(key)

    if cleaned_content == msg.content and tool_calls == list(msg.tool_calls or []):
        return msg

    extra = dict(msg.additional_kwargs or {})
    extra.pop("tool_calls", None)
    extra.pop("function_call", None)

    return AIMessage(
        content=cleaned_content,
        id=msg.id,
        tool_calls=tool_calls,
        usage_metadata=msg.usage_metadata,
        response_metadata=dict(msg.response_metadata or {}),
        additional_kwargs=extra,
    )
