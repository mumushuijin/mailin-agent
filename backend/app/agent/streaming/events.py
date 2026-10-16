from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.agent.state import validate_agent_state


_TURN_SEQUENCES: dict[str, int] = defaultdict(int)


@dataclass
class AgentEvent:
    """统一的 Agent 流式事件，SSE 与 WebSocket 共用。"""

    type: str
    data: dict
    turn_id: str | None = None
    state: dict[str, Any] | None = None
    event_id: str | None = None
    seq: int | None = None

    def with_turn_id(self, turn_id: str | None) -> AgentEvent:
        if turn_id is None or self.turn_id == turn_id:
            return self
        return AgentEvent(
            type=self.type, data=self.data, turn_id=turn_id, state=self.state,
            event_id=self.event_id, seq=self.seq,
        )

    def ensure_identity(self) -> None:
        if not self.event_id:
            self.event_id = f"evt_{uuid.uuid4().hex}"
        if self.seq is None and self.turn_id:
            _TURN_SEQUENCES[self.turn_id] += 1
            self.seq = _TURN_SEQUENCES[self.turn_id]

    def to_state(self) -> dict[str, Any] | None:
        if self.state is None:
            return None
        validate_agent_state(self.state)
        return self.state


def to_sse(event: AgentEvent) -> str:
    event.ensure_identity()
    data = dict(event.data)
    if event.turn_id:
        data["turn_id"] = event.turn_id
    if event.event_id:
        data["event_id"] = event.event_id
    if event.seq is not None:
        data["seq"] = event.seq
    state = event.to_state()
    if state is not None:
        data["state"] = state
    return f"event: {event.type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def to_ws(event: AgentEvent) -> dict:
    event.ensure_identity()
    payload: dict = {"type": event.type, "data": event.data}
    if event.turn_id:
        payload["turn_id"] = event.turn_id
    if event.event_id:
        payload["event_id"] = event.event_id
    if event.seq is not None:
        payload["seq"] = event.seq
    state = event.to_state()
    if state is not None:
        payload["state"] = state
    return payload


def format_sse(event_type: str, data: dict) -> str:
    """兼容旧调用方。"""
    return to_sse(AgentEvent(type=event_type, data=data))
