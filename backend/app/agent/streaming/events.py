from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class AgentEvent:
    """统一的 Agent 流式事件，SSE 与 WebSocket 共用。"""

    type: str
    data: dict
    run_id: str | None = None

    def with_run_id(self, run_id: str | None) -> AgentEvent:
        if run_id is None or self.run_id == run_id:
            return self
        return AgentEvent(type=self.type, data=self.data, run_id=run_id)


def to_sse(event: AgentEvent) -> str:
    data = dict(event.data)
    if event.run_id:
        data["run_id"] = event.run_id
    return f"event: {event.type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def to_ws(event: AgentEvent) -> dict:
    payload: dict = {"type": event.type, "data": event.data}
    if event.run_id:
        payload["run_id"] = event.run_id
    return payload


def format_sse(event_type: str, data: dict) -> str:
    """兼容旧调用方。"""
    return to_sse(AgentEvent(type=event_type, data=data))
