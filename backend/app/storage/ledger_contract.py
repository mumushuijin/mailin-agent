"""Identity and validation contract for new-format conversation ledger rows."""
from __future__ import annotations

from typing import Any, Literal, NewType, TypedDict


Origin = Literal["user", "assistant", "tool", "system_maintenance"]
MessageId = NewType("MessageId", str)  # Application ledger event
VendorMessageId = NewType("VendorMessageId", str)  # Serialized message.data.id
ToolCallId = NewType("ToolCallId", str)  # Tool call/result pairing
RequestId = NewType("RequestId", str)  # One actual model HTTP/API request
EventId = NewType("EventId", str)  # One transport event
ORIGINS: set[str] = {"user", "assistant", "tool", "system_maintenance"}
SCOPE_KEYS = ("workspace_id", "session_id", "run_id", "task_id", "request_id", "step_id")


class LedgerScope(TypedDict):
    workspace_id: str
    session_id: str
    run_id: str
    task_id: str | None
    request_id: str | None
    step_id: str | None


class LedgerRow(TypedDict):
    seq: int
    message_id: str
    scope: LedgerScope
    origin: Origin
    message: dict[str, Any]


def validate_scope(value: Any, *, session_id: str | None = None) -> LedgerScope:
    if not isinstance(value, dict) or set(value) != set(SCOPE_KEYS):
        raise ValueError("ledger scope must contain exactly six identity fields")
    for key in SCOPE_KEYS[:3]:
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"ledger scope.{key} must be a non-empty string")
    for key in SCOPE_KEYS[3:]:
        if value[key] is not None and (not isinstance(value[key], str) or not value[key]):
            raise ValueError(f"ledger scope.{key} must be a string or null")
    if session_id is not None and value["session_id"] != session_id:
        raise ValueError("ledger scope.session_id does not match session")
    return value


def validate_row(value: Any, *, session_id: str | None = None) -> LedgerRow:
    if not isinstance(value, dict) or set(value) != {"seq", "message_id", "scope", "origin", "message"}:
        raise ValueError("ledger row must contain seq, message_id, scope, origin, message")
    if type(value["seq"]) is not int or value["seq"] <= 0:
        raise ValueError("ledger seq must be a positive integer")
    if not isinstance(value["message_id"], str) or not value["message_id"]:
        raise ValueError("ledger message_id must be a non-empty string")
    validate_scope(value["scope"], session_id=session_id)
    if not isinstance(value["origin"], str) or value["origin"] not in ORIGINS:
        raise ValueError("ledger origin is invalid")
    if not isinstance(value["message"], dict) or not isinstance(value["message"].get("type"), str) or not isinstance(value["message"].get("data"), dict):
        raise ValueError("ledger message must be a serialized LangChain message")
    return value
