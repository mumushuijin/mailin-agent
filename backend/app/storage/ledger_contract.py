"""Versioned event and message projection contracts for session ledgers."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any, NewType

LEDGER_SCHEMA_VERSION = 3
PROJECTION_VERSION = 4
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
    "activity": {"pending", "running", "waiting", "completed", "failed", "cancelled"},
    "interaction": {"waiting", "completed", "failed", "cancelled", "denied", "expired"},
    "context": {"running", "completed", "failed", "succeeded"},
}
EVENT_TYPES = {
    "message.accepted",
    "task.started", "task.settled",
    "step.started", "step.settled",
    "activity.started", "activity.settled",
    "model.request.started", "model.response.completed", "model.request.failed", "model.request.interrupted",
    "tool.attempt.started", "tool.attempt.settled",
    "interaction.opened", "interaction.resolved",
    "memory.sync.recorded", "turn.settled", "context.window.materialized", "branch.forked",
}
_ENVELOPE_REQUIRED = {
    "schema_version", "session_id", "event_seq", "event_id", "recorded_at", "occurred_at",
    "branch_id", "event_type", "payload",
}
_ENVELOPE_OPTIONAL = {
    "turn_id", "activity_id", "parent_activity_id", "causation_event_id", "request_id",
    "batch_id", "call_id", "attempt_id", "interaction_id",
}
_EVENT_ID_FIELDS = {
    "turn_id", "activity_id", "parent_activity_id", "causation_event_id", "request_id",
    "batch_id", "call_id", "attempt_id", "interaction_id",
}


def _identifier(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _milliseconds(value: Any, field: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be UTC Unix milliseconds")


def _timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc


def validate_scope(value: Any, *, session_id: str | None = None) -> dict[str, str]:
    """Validate checkpoint association, never a persisted ledger event."""
    if not isinstance(value, dict) or set(value) != set(SCOPE_KEYS):
        raise ValueError("unsupported scope format: expected workspace/session/turn")
    for key in SCOPE_KEYS:
        _identifier(value[key], f"scope.{key}")
    if session_id is not None and value["session_id"] != session_id:
        raise ValueError("scope.session_id does not match session")
    return value


def validate_metadata(value: Any, *, workspace_id: str, session_id: str) -> dict[str, Any]:
    required = {"schema_version", "workspace_id", "session_id", "resource_layout"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("unsupported ledger metadata format")
    if type(value["schema_version"]) is not int or value["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise ValueError("unsupported ledger schema version; existing data is preserved")
    if value["workspace_id"] != workspace_id or value["session_id"] != session_id:
        raise ValueError("ledger metadata container identity does not match workspace/session")
    if value["resource_layout"] != "session-v1":
        raise ValueError("unsupported ledger resource layout")
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
    """Validate the internal message projection used by existing runtime consumers."""
    del session_id
    required = {"seq", "message_id", "timestamp", "message"}
    optional = {"turn_id", "model", "execution", "interaction", "extensions"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - optional:
        raise ValueError("unsupported ledger projection row format")
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


def _assert_no_hidden_reasoning(value: Any, path: str = "payload") -> None:
    forbidden = {"reasoning_content", "chain_of_thought", "hidden_reasoning", "private_reasoning"}
    hidden_blocks = {"reasoning", "thinking", "redacted_thinking", "analysis"}
    if isinstance(value, dict):
        if value.get("type") in hidden_blocks:
            raise ValueError(f"{path} must not contain hidden reasoning blocks")
        for key, child in value.items():
            if key.lower() in forbidden:
                raise ValueError(f"{path} must not contain hidden reasoning fields")
            _assert_no_hidden_reasoning(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_hidden_reasoning(child, f"{path}[{index}]")


def validate_event(value: Any, *, session_id: str | None = None, expected_seq: int | None = None) -> dict[str, Any]:
    """Validate an immutable versioned event envelope and its event-specific identity."""
    if not isinstance(value, dict) or not _ENVELOPE_REQUIRED.issubset(value) or set(value) - _ENVELOPE_REQUIRED - _ENVELOPE_OPTIONAL:
        raise ValueError("unsupported event envelope format")
    if type(value["schema_version"]) is not int or value["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise ValueError("unsupported event schema version")
    if type(value["event_seq"]) is not int or value["event_seq"] <= 0:
        raise ValueError("event_seq must be a positive integer")
    if expected_seq is not None and value["event_seq"] != expected_seq:
        raise ValueError("event sequence integrity error")
    _identifier(value["session_id"], "session_id")
    if session_id is not None and value["session_id"] != session_id:
        raise ValueError("event session_id does not match session")
    _identifier(value["event_id"], "event_id")
    _identifier(value["branch_id"], "branch_id")
    _timestamp(value["recorded_at"], "recorded_at")
    _timestamp(value["occurred_at"], "occurred_at")
    if value["event_type"] not in EVENT_TYPES:
        raise ValueError(f"unsupported event type: {value['event_type']}")
    for field in _EVENT_ID_FIELDS & set(value):
        _identifier(value[field], field)
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    _assert_no_hidden_reasoning(payload)
    event_type = value["event_type"]
    runtime = ((payload.get("extensions") or {}).get("runtime") or {})
    projection_identity_fields = {"message_id", "timestamp", "message"}
    is_message_projection = projection_identity_fields.issubset(payload)
    # Message projection rows carry their own validated compatibility identity;
    # native event payloads must name the domain entity they change explicitly.
    if not is_message_projection:
        if event_type in {"task.started", "task.settled", "step.started", "step.settled"}:
            kind = event_type.split(".")[0]
            _identifier(payload.get(f"{kind}_id") or (payload.get(kind) or {}).get("id") or runtime.get(f"{kind}_id"), f"payload.{kind}_id")
        if event_type.startswith("activity."):
            _identifier(value.get("activity_id") or payload.get("activity_id") or runtime.get("activity_id"), "activity_id")
    if event_type == "message.accepted":
        _identifier(payload.get("submission_id"), "payload.submission_id")
        if (payload.get("message") or {}).get("role") != "user":
            raise ValueError("message.accepted requires a user message")
    elif event_type == "model.response.completed":
        if (payload.get("message") or {}).get("role") not in {"assistant", "system"}:
            raise ValueError("model.response.completed requires an assistant response")
        if not is_message_projection:
            _identifier(value.get("request_id") or (payload.get("model") or {}).get("request_id"), "request_id")
    elif event_type.startswith("model.request.") or event_type == "model.response.completed":
        _identifier(value.get("request_id") or (payload.get("model") or {}).get("request_id"), "request_id")
    if event_type.startswith("tool.attempt."):
        _identifier(value.get("call_id") or (payload.get("message") or {}).get("tool_call_id"), "call_id")
        if not value.get("attempt_id") and not is_message_projection:
            execution = payload.get("execution")
            if not isinstance(execution, dict) or "attempt" not in execution:
                raise ValueError("native tool events require attempt_id or execution.attempt")
            if type(execution["attempt"]) is not int or execution["attempt"] <= 0:
                raise ValueError("execution.attempt must be a positive integer")
    if event_type.startswith("interaction."):
        _identifier(value.get("interaction_id") or (payload.get("interaction") or {}).get("id"), "interaction_id")
    if event_type == "branch.forked":
        _identifier(payload.get("parent_branch_id"), "payload.parent_branch_id")
        cursor = payload.get("parent_cursor")
        if not isinstance(cursor, dict) or set(cursor) != {"branch_id", "event_seq", "event_id"}:
            raise ValueError("branch.forked requires a parent event cursor")
        _identifier(cursor["branch_id"], "payload.parent_cursor.branch_id")
        _identifier(cursor["event_id"], "payload.parent_cursor.event_id")
        if type(cursor["event_seq"]) is not int or cursor["event_seq"] < 0:
            raise ValueError("payload.parent_cursor.event_seq must be non-negative")
    if event_type == "context.window.materialized":
        for field in ("window_schema_version", "compression_version", "content_sha256", "bootstrap_fingerprint", "reference_fingerprint"):
            _identifier(payload.get(field), f"payload.{field}")
        if not isinstance(payload.get("source_event_cursor"), dict):
            raise ValueError("context window requires source_event_cursor")
        if "window_ref" not in payload:
            raise ValueError("context window requires window_ref")
        if "window_ref" in payload:
            reference = payload["window_ref"]
            if not isinstance(reference, dict) or set(reference) != {"type", "path", "media_type", "bytes", "sha256"}:
                raise ValueError("context window requires a complete window_ref")
            if reference.get("type") != "reference" or reference.get("media_type") != "application/json":
                raise ValueError("context window window_ref must be an application/json reference")
            if not isinstance(reference.get("path"), str) or not reference["path"].startswith("content/windows/"):
                raise ValueError("context window window_ref must point to content/windows")
            if type(reference.get("bytes")) is not int or reference["bytes"] < 0:
                raise ValueError("context window window_ref.bytes must be non-negative")
            if not isinstance(reference.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]):
                raise ValueError("context window window_ref.sha256 must be a lowercase SHA-256 digest")
    if event_type == "memory.sync.recorded":
        if type(payload.get("recorded_count")) is not int or payload["recorded_count"] < 0:
            raise ValueError("memory sync requires recorded_count")
        _identifier(payload.get("last_synced_turn"), "payload.last_synced_turn")
        if not isinstance(payload.get("source_files"), list):
            raise ValueError("memory sync requires source_files")
    if projection_identity_fields & set(payload) and not is_message_projection:
        raise ValueError("message projection requires message_id, timestamp, and message")
    row_fields = {"message_id", "timestamp", "message", "turn_id", "model", "execution", "interaction", "extensions"}
    if is_message_projection:
        row = {key: payload[key] for key in row_fields if key in payload}
        if value.get("turn_id") and "turn_id" not in row:
            row["turn_id"] = value["turn_id"]
        row["seq"] = value["event_seq"]
        validate_row(row)
    return value
