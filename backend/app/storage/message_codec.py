"""Explicit adapter between business ledger messages and LangChain objects."""
from __future__ import annotations

import copy
import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.core.logging import redact_log_value

_PRIVATE_KEYS = {"reasoning", "reasoning_content", "thinking", "analysis", "reasoning_details"}
_PRIVATE_BLOCKS = {"reasoning", "thinking", "redacted_thinking", "analysis"}
_IDENTITY_KEYS = {"id", "message_id", "turn_id", "request_id", "call_id", "tool_call_id", "step_id", "task_id", "path", "sha256", "media_type"}
_INTERNAL_KEYS = {
    "ledger_message_id", "ledger_timestamp", "timestamp", "ledger_model", "ledger_execution",
    "ledger_interaction", "ledger_extensions", "ledger_turn_id", "ledger_seq", "ledger_reference",
    "message_id", "vendor_message_id", "request_id", "run_id", "turn_id", "scope", "origin",
    "tool_status", "reason_code", "system_maintenance", "kind",
    "tool_cache_path", "tool_cache_summary", "skip_tool_cache",
}


def safe_business_value(value: Any) -> Any:
    """Remove hidden reasoning and reuse the application's credential filtering."""
    if isinstance(value, dict):
        filtered = {}
        for key, item in value.items():
            lowered = str(key).lower()
            # Reasoning token counts are allowed; hidden reasoning text is not.
            if lowered in _PRIVATE_KEYS and type(item) is not int:
                continue
            if lowered in _IDENTITY_KEYS:
                filtered[key] = copy.deepcopy(item)
            elif lowered.endswith("_tokens") or lowered.endswith("_token_details"):
                filtered[key] = copy.deepcopy(item)
            elif re.search(r"api[_-]?key|secret|password|authorization|(?:^|_)token$", lowered):
                filtered[key] = "***"
            else:
                filtered[key] = safe_business_value(item)
        return filtered
    if isinstance(value, list):
        return [safe_business_value(item) for item in value if not (isinstance(item, dict) and item.get("type") in _PRIVATE_BLOCKS)]
    if isinstance(value, str):
        return re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer ***", redact_log_value(value))
    return copy.deepcopy(value)


def _usage(value: dict[str, Any]) -> dict[str, Any]:
    result = {key: copy.deepcopy(item) for key, item in value.items() if key != "total_tokens" and item is not None}
    # Providers sometimes use legacy names; each stored counter has one name.
    for old, new in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        if old in result:
            result.setdefault(new, result.pop(old))
    return result


def encode_message(message: BaseMessage) -> dict[str, Any]:
    role = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}.get(message.type)
    if role is None:
        raise ValueError(f"unsupported durable message type: {message.type}")
    kwargs = message.additional_kwargs or {}
    payload: dict[str, Any] = {"role": role}
    if message.id:
        payload["id"] = message.id
    content = safe_business_value(message.content)
    if isinstance(content, list):
        content = [{"type": "text", "text": block} if isinstance(block, str) else block for block in content]
    calls = getattr(message, "tool_calls", None) or []
    if content or not calls:
        payload["content"] = content
    if calls:
        payload["tool_calls"] = [
            {"id": call["id"], "name": call["name"], "arguments": safe_business_value(call.get("args", {}))}
            for call in calls
        ]
    if isinstance(message, ToolMessage):
        payload["tool_call_id"] = message.tool_call_id
    result: dict[str, Any] = {"message": payload}
    extensions = copy.deepcopy(kwargs.get("ledger_extensions") or {})
    framework: dict[str, Any] = {}
    extras = {key: value for key, value in kwargs.items() if key not in _INTERNAL_KEYS and key not in _PRIVATE_KEYS and key != "tool_calls"}
    for key in ("attempt", "started_at", "finished_at"):
        extras.pop(key, None)
    if extras:
        framework["additional_kwargs"] = safe_business_value(extras)
    metadata = dict(message.response_metadata or {})
    model = copy.deepcopy(kwargs.get("ledger_model") or {})
    request_id = model.get("request_id") or kwargs.get("request_id")
    provider_usage = metadata.pop("token_usage", None) or metadata.pop("usage", None)
    usage = getattr(message, "usage_metadata", None) or provider_usage
    finish_reason = metadata.pop("finish_reason", None)
    model_name = metadata.pop("model_name", None)
    if request_id:
        model["request_id"] = request_id
        if finish_reason is not None:
            model.setdefault("finish_reason", finish_reason)
        if model_name:
            model.setdefault("name", model_name)
        if usage:
            model.setdefault("usage", _usage(usage))
        if model.get("usage"):
            model["usage"] = _usage(model["usage"])
        result["model"] = model
    else:
        # A standalone framework message can lack request provenance. Retain its
        # available metadata without claiming an actual application request.
        if usage:
            framework["usage_metadata"] = _usage(usage)
        if finish_reason is not None:
            metadata["finish_reason"] = finish_reason
        if model_name:
            metadata["model_name"] = model_name
    for alias in ("request_id", "message_id", "run_id", "turn_id", "id"):
        metadata.pop(alias, None)
    if metadata:
        framework["response_metadata"] = safe_business_value(metadata)
    invalid = getattr(message, "invalid_tool_calls", None)
    if invalid:
        framework["invalid_tool_calls"] = safe_business_value(invalid)
    artifact = getattr(message, "artifact", None)
    if artifact is not None:
        framework["artifact"] = safe_business_value(artifact)
    if message.model_extra:
        framework["fields"] = safe_business_value(message.model_extra)
    if message.name and role != "tool":
        framework["name"] = message.name
    if framework:
        extensions["langchain"] = {**extensions.get("langchain", {}), **framework}
    if extensions:
        result["extensions"] = extensions
    execution = copy.deepcopy(kwargs.get("ledger_execution") or {})
    if role == "tool" and not execution:
        status = kwargs.get("tool_status") or ("failed" if kwargs.get("reason_code") or getattr(message, "status", None) == "error" else "succeeded")
        execution["status"] = "succeeded" if status == "completed" else status
        if kwargs.get("reason_code"):
            execution["error"] = {"code": str(kwargs["reason_code"])}
        for key in ("attempt", "started_at", "finished_at"):
            if kwargs.get(key) is not None:
                execution[key] = kwargs[key]
        if "finished_at" not in execution and kwargs.get("ledger_timestamp") is not None:
            execution["finished_at"] = kwargs["ledger_timestamp"]
        # A settlement never copies the start timestamp from a runtime record.
        execution.pop("started_at", None)
    if role == "assistant" and request_id and not execution:
        execution = {"status": "succeeded"}
        if kwargs.get("ledger_timestamp") is not None:
            execution["finished_at"] = kwargs["ledger_timestamp"]
    if execution:
        result["execution"] = safe_business_value(execution)
    if kwargs.get("ledger_interaction"):
        result["interaction"] = safe_business_value(kwargs["ledger_interaction"])
    return result


def decode_message(row: dict[str, Any]) -> BaseMessage:
    """Restore model content while retaining application identity only in memory."""
    payload = copy.deepcopy(row["message"])
    role = payload["role"]
    framework = copy.deepcopy((row.get("extensions") or {}).get("langchain") or {})
    kwargs = framework.get("additional_kwargs") or {}
    kwargs.update({"ledger_message_id": row["message_id"], "ledger_timestamp": row["timestamp"], "ledger_seq": row["seq"]})
    for name in ("model", "execution", "interaction", "extensions"):
        if row.get(name):
            kwargs[f"ledger_{name}"] = copy.deepcopy(row[name])
    if row.get("turn_id"):
        kwargs["ledger_turn_id"] = row["turn_id"]
    if role == "system":
        kwargs["system_maintenance"] = True
    model = row.get("model") or {}
    metadata = framework.get("response_metadata") or {}
    if model.get("finish_reason") is not None:
        metadata["finish_reason"] = model["finish_reason"]
    if model.get("name"):
        metadata["model_name"] = model["name"]
    common = {"content": payload.get("content", ""), "id": payload.get("id"), "additional_kwargs": kwargs, "response_metadata": metadata}
    if framework.get("name"):
        common["name"] = framework["name"]
    if framework.get("fields"):
        common.update(framework["fields"])
    if role == "assistant":
        common["tool_calls"] = [{"id": call["id"], "name": call["name"], "args": copy.deepcopy(call["arguments"]), "type": "tool_call"} for call in payload.get("tool_calls", [])]
        if framework.get("invalid_tool_calls"):
            common["invalid_tool_calls"] = framework["invalid_tool_calls"]
        usage = copy.deepcopy(model.get("usage") or framework.get("usage_metadata"))
        if usage and "input_tokens" in usage and "output_tokens" in usage:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
            common["usage_metadata"] = usage
        return AIMessage(**common)
    if role == "tool":
        execution = row.get("execution") or {}
        kwargs["tool_status"] = execution.get("status", "succeeded")
        if execution.get("error", {}).get("code"):
            kwargs["reason_code"] = execution["error"]["code"]
        return ToolMessage(**common, tool_call_id=payload["tool_call_id"], artifact=framework.get("artifact"), status="error" if execution.get("status") in {"failed", "unknown"} else "success")
    if role == "user":
        return HumanMessage(**common)
    return SystemMessage(**common)
