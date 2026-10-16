"""Versioned, framework-independent contract for conversation messages."""
from __future__ import annotations

from typing import Any, NewType

LEDGER_SCHEMA_VERSION = 2
MessageId = NewType("MessageId", str)
ToolCallId = NewType("ToolCallId", str)
RequestId = NewType("RequestId", str)
EventId = NewType("EventId", str)
SCOPE_KEYS = ("workspace_id", "session_id", "turn_id")
ROLES = {"user", "assistant", "tool", "system"}
EXECUTION_STATUSES = {
    "tool": {"running", "succeeded", "failed", "cancelled", "denied", "skipped", "unknown"},
    "model_request": {"running", "succeeded", "failed", "cancelled", "unknown"},
    "turn": {"completed", "failed", "cancelled", "handoff"},
    "step": {"pending", "running", "waiting", "completed", "failed", "cancelled"},
    "task": {"pending", "running", "waiting", "completed", "failed", "cancelled", "skipped"},
    "interaction": {"waiting", "completed", "failed", "cancelled", "denied", "expired"},
    "context": {"running", "completed", "failed", "succeeded"},
}


def _identifier(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _milliseconds(value: Any, field: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be UTC Unix milliseconds")


def validate_scope(value: Any, *, session_id: str | None = None) -> dict[str, str]:
    """Validate checkpoint association, never a persisted message row."""
    if not isinstance(value, dict) or set(value) != set(SCOPE_KEYS):
        raise ValueError("unsupported scope format: expected workspace/session/turn")
    for key in SCOPE_KEYS:
        _identifier(value[key], f"scope.{key}")
    if session_id is not None and value["session_id"] != session_id:
        raise ValueError("scope.session_id does not match session")
    return value


def validate_metadata(value: Any, *, workspace_id: str, session_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "workspace_id", "session_id"}:
        raise ValueError("unsupported ledger metadata format")
    if type(value["schema_version"]) is not int or value["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise ValueError("unsupported ledger schema version")
    if value["workspace_id"] != workspace_id or value["session_id"] != session_id:
        raise ValueError("ledger metadata container identity does not match workspace/session")
    return value


def lifecycle_subject(value: dict[str, Any]) -> str | None:
    message = value.get("message") or {}
    content = message.get("content")
    if message.get("role") == "system" and isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "lifecycle":
                return block.get("subject")
    return None


def is_audit_row(value: dict[str, Any]) -> bool:
    return value.get("message", {}).get("role") == "system" or bool(
        (value.get("extensions") or {}).get("runtime", {}).get("auxiliary")
    )


def validate_row(value: Any, *, session_id: str | None = None) -> dict[str, Any]:
    del session_id  # Container identity has one authoritative metadata file.
    required = {"seq", "message_id", "timestamp", "message"}
    optional = {"turn_id", "model", "execution", "interaction", "extensions"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - optional:
        raise ValueError("unsupported ledger row format: expected a single business message")
    if type(value["seq"]) is not int or value["seq"] <= 0:
        raise ValueError("ledger seq must be a positive integer")
    _identifier(value["message_id"], "message_id")
    _milliseconds(value["timestamp"], "timestamp")
    if "turn_id" in value:
        _identifier(value["turn_id"], "turn_id")
    message = value["message"]
    if not isinstance(message, dict) or message.get("role") not in ROLES:
        raise ValueError("message.role must be user/assistant/tool/system")
    if set(message) - {"role", "id", "content", "tool_calls", "tool_call_id"}:
        raise ValueError("message has unsupported fields or identity aliases")
    if message["role"] != "system" and "turn_id" not in value:
        raise ValueError("conversation messages require turn_id")
    for key in ("id", "tool_call_id"):
        if key in message:
            _identifier(message[key], f"message.{key}")
    if "content" in message:
        content = message["content"]
        if not isinstance(content, (str, list)):
            raise ValueError("message.content must be text or typed content blocks")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or not isinstance(block.get("type"), str) or not block["type"]:
                    raise ValueError("message content blocks require type")
                if block["type"] == "reference":
                    if set(block) != {"type", "path", "media_type", "bytes", "sha256"}:
                        raise ValueError("content reference requires path/media_type/bytes/sha256")
                    _identifier(block["path"], "reference.path")
                    _identifier(block["media_type"], "reference.media_type")
                    if type(block["bytes"]) is not int or block["bytes"] < 0:
                        raise ValueError("reference.bytes must be non-negative")
                    if not isinstance(block["sha256"], str) or len(block["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in block["sha256"]):
                        raise ValueError("reference.sha256 must be a SHA-256 hex digest")
                if block["type"] == "lifecycle" and (message["role"] != "system" or block.get("subject") not in EXECUTION_STATUSES):
                    raise ValueError("lifecycle blocks require a supported system subject")
            references = [block for block in content if block["type"] == "reference"]
            if references and (len(references) != 1 or len(content) != 1):
                raise ValueError("inline and referenced content are mutually exclusive")
    elif message["role"] != "assistant" or not message.get("tool_calls"):
        raise ValueError("message requires content unless it defines assistant tool calls")
    if "tool_calls" in message:
        if message["role"] != "assistant" or not isinstance(message["tool_calls"], list) or not message["tool_calls"]:
            raise ValueError("tool_calls require a non-empty assistant call definition")
        calls: set[str] = set()
        for call in message["tool_calls"]:
            if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"} or not isinstance(call["arguments"], dict):
                raise ValueError("tool call requires id/name/arguments object")
            _identifier(call["id"], "tool_call.id")
            _identifier(call["name"], "tool_call.name")
            if call["id"] in calls:
                raise ValueError("duplicate tool call id in assistant message")
            calls.add(call["id"])
    if message["role"] == "tool" and "tool_call_id" not in message:
        raise ValueError("tool result requires tool_call_id")
    if message["role"] == "tool" and ("execution" not in value or value["execution"].get("status") == "running"):
        raise ValueError("final tool messages require a settled execution outcome")
    subject = lifecycle_subject(value)
    if subject is not None and subject not in EXECUTION_STATUSES:
        raise ValueError("unsupported lifecycle subject")
    if subject == "tool" and "tool_call_id" not in message:
        raise ValueError("tool lifecycle requires tool_call_id")
    if subject == "model_request" and "model" not in value:
        raise ValueError("model lifecycle requires request metadata")
    if subject and subject != "context" and "turn_id" not in value:
        raise ValueError("turn lifecycle messages require turn_id")
    for field in optional - {"turn_id"}:
        if field in value and (not isinstance(value[field], dict) or not value[field]):
            raise ValueError(f"{field} must be an applicable non-empty object")
    if "model" in value:
        model = value["model"]
        if set(model) - {"request_id", "name", "finish_reason", "usage"}:
            raise ValueError("model has unsupported fields")
        _identifier(model.get("request_id"), "model.request_id")
        if message["role"] not in {"assistant", "system"}:
            raise ValueError("model metadata does not belong to user/tool messages")
        if "usage" in model:
            usage = model["usage"]
            if not isinstance(usage, dict) or not usage or "total_tokens" in usage:
                raise ValueError("model.usage must omit derived total_tokens")
            def validate_counts(counts: dict[str, Any]) -> None:
                for name, count in counts.items():
                    if isinstance(count, dict):
                        validate_counts(count)
                    elif type(count) is not int or count < 0:
                        raise ValueError(f"model.usage.{name} must be an actual non-negative count")
            validate_counts(usage)
        for key in ("name", "finish_reason"):
            if key in model:
                _identifier(model[key], f"model.{key}")
    if "execution" in value:
        execution = value["execution"]
        if set(execution) - {"status", "attempt", "started_at", "finished_at", "error"}:
            raise ValueError("execution has unsupported fields")
        domain = subject or ("tool" if message["role"] == "tool" else "interaction" if value.get("interaction") else "model_request")
        if execution.get("status") not in EXECUTION_STATUSES[domain]:
            raise ValueError(f"invalid {domain} execution status")
        for key in ("started_at", "finished_at"):
            if key in execution:
                _milliseconds(execution[key], f"execution.{key}")
        if "started_at" in execution and "finished_at" in execution:
            raise ValueError("start and settlement timestamps belong to separate messages")
        if "attempt" in execution and (type(execution["attempt"]) is not int or execution["attempt"] <= 0):
            raise ValueError("execution.attempt must be a positive integer")
        if execution["status"] in {"denied", "skipped"} and ({"attempt", "started_at"} & set(execution)):
            raise ValueError("unexecuted outcomes cannot claim an attempt/start")
        if "error" in execution:
            if not isinstance(execution["error"], dict):
                raise ValueError("execution.error must be structured")
            _identifier(execution["error"].get("code"), "execution.error.code")
    if "interaction" in value:
        _identifier(value["interaction"].get("id"), "interaction.id")
        _identifier(value["interaction"].get("kind"), "interaction.kind")
    if "extensions" in value:
        if any(key in required | optional | {"scope", "origin", "vendor_message_id", "run_id"} for key in value["extensions"]):
            raise ValueError("extensions must use namespaces without standard identity aliases")
        if any(not isinstance(item, dict) for item in value["extensions"].values()):
            raise ValueError("extensions namespaces must be objects")
        kwargs = value["extensions"].get("langchain", {}).get("additional_kwargs", {})
        if isinstance(kwargs, dict) and set(kwargs) & {"ledger_message_id", "message_id", "request_id", "run_id", "turn_id", "scope", "origin", "vendor_message_id"}:
            raise ValueError("framework extensions cannot repeat standard identities")
    return value
