from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.agent.state import validate_agent_state

STREAM_SCHEMA_VERSION = 3

@dataclass
class AgentEvent:
    """统一的 Agent 流式事件，SSE 与 WebSocket 共用。"""

    type: str
    data: dict
    turn_id: str | None = None
    state: dict[str, Any] | None = None
    event_id: str | None = None
    event_seq: int | None = None
    branch_id: str | None = None
    transport_id: str | None = None
    transport_seq: int | None = None
    state_revision: int | None = None
    # Deprecated in-process alias retained while older UI consumers migrate.
    seq: int | None = None

    def with_turn_id(self, turn_id: str | None) -> AgentEvent:
        if turn_id is None or self.turn_id == turn_id:
            return self
        return AgentEvent(
            type=self.type, data=self.data, turn_id=turn_id, state=self.state,
            event_id=self.event_id, event_seq=self.event_seq, branch_id=self.branch_id,
            transport_id=self.transport_id, transport_seq=self.transport_seq,
            state_revision=self.state_revision, seq=self.seq,
        )

    def ensure_identity(self) -> None:
        if not self.transport_id:
            self.transport_id = f"frame_{uuid.uuid4().hex}"
        if self.transport_seq is None and self.seq is not None:
            self.transport_seq = self.seq
        if self.seq is None:
            self.seq = self.transport_seq
        if self.state is not None:
            self.state_revision = int(self.state.get("state_revision", 0))
            pointer = self.state.get("ledger_pointer") or {}
            cursor = pointer.get("event_cursor") or {}
            if cursor:
                self.branch_id = self.branch_id or cursor.get("branch_id")

    def to_state(self) -> dict[str, Any] | None:
        if self.state is None:
            return None
        validate_agent_state(self.state)
        return self.state


def to_sse(event: AgentEvent, *, transport_id: str | None = None, transport_seq: int | None = None) -> str:
    if transport_id is not None:
        event.transport_id = transport_id
    if transport_seq is not None:
        event.transport_seq = transport_seq
        event.seq = transport_seq
    event.ensure_identity()
    data = dict(event.data)
    data["stream_schema_version"] = STREAM_SCHEMA_VERSION
    if event.turn_id:
        data["turn_id"] = event.turn_id
    if event.event_id:
        data["event_id"] = event.event_id
    if event.event_seq is not None:
        data["event_seq"] = event.event_seq
    if event.branch_id:
        data["branch_id"] = event.branch_id
    if event.transport_id:
        data["transport_id"] = event.transport_id
    if event.transport_seq is not None:
        data["transport_seq"] = event.transport_seq
    if event.state_revision is not None:
        data["state_revision"] = event.state_revision
    if event.seq is not None:
        data["seq"] = event.seq
    state = event.to_state()
    if state is not None:
        data["state"] = state
    return f"event: {event.type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def to_ws(event: AgentEvent, *, transport_id: str | None = None, transport_seq: int | None = None) -> dict:
    if transport_id is not None:
        event.transport_id = transport_id
    if transport_seq is not None:
        event.transport_seq = transport_seq
        event.seq = transport_seq
    event.ensure_identity()
    payload: dict = {"type": event.type, "data": event.data, "stream_schema_version": STREAM_SCHEMA_VERSION}
    if event.turn_id:
        payload["turn_id"] = event.turn_id
    if event.event_id:
        payload["event_id"] = event.event_id
    if event.event_seq is not None:
        payload["event_seq"] = event.event_seq
    if event.branch_id:
        payload["branch_id"] = event.branch_id
    if event.transport_id:
        payload["transport_id"] = event.transport_id
    if event.transport_seq is not None:
        payload["transport_seq"] = event.transport_seq
    if event.state_revision is not None:
        payload["state_revision"] = event.state_revision
    if event.seq is not None:
        payload["seq"] = event.seq
    state = event.to_state()
    if state is not None:
        payload["state"] = state
    return payload


def format_sse(event_type: str, data: dict) -> str:
    """兼容旧调用方。"""
    return to_sse(AgentEvent(type=event_type, data=data))
