"""Per-run ordered, non-blocking lifecycle dispatcher."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class DispatchEvent:
    run_id: str
    seq: int
    kind: str
    payload: dict[str, Any]


class RunEventDispatcher:
    def __init__(self, run_id: str, *, maxsize: int = 128):
        self.run_id = run_id
        self._queue: asyncio.Queue[DispatchEvent] = asyncio.Queue(maxsize=maxsize)
        self._seq = 0
        self._latest_state: Any = None
        self._needs_snapshot = False
        self._closed = False

    @property
    def latest_state(self) -> dict[str, Any] | None:
        return self._latest_state

    def publish_nowait(self, kind: str, payload: dict[str, Any], *, state: dict[str, Any] | None = None) -> bool:
        if self._closed:
            return False
        self._seq += 1
        if state is not None:
            self._latest_state = state
        event = DispatchEvent(self.run_id, self._seq, kind, dict(payload))
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            # Control events may be coalesced, but text chunks are never silently dropped.
            if state is not None:
                self._latest_state = state
                self._needs_snapshot = True
            if kind == "llm_chunk":
                raise
            return False

    async def publish(self, kind: str, payload: dict[str, Any], *, state: Any = None) -> bool:
        """Queue an event without waiting for consumers except to preserve text chunks."""
        if self._closed:
            return False
        self._seq += 1
        if state is not None:
            self._latest_state = state
        event = DispatchEvent(self.run_id, self._seq, kind, dict(payload))
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            if state is not None:
                self._latest_state = state
                self._needs_snapshot = True
            if kind in {
                "chunk", "llm_chunk", "session", "interrupt", "done", "error",
                "step_start", "step_finish", "tool_start", "tool_finish",
            } or state is None:
                await self._queue.put(event)
                return True
            return False

    async def run(self, send: Callable[[DispatchEvent], Awaitable[None]]) -> None:
        while True:
            if self._closed and self._queue.empty():
                return
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            await send(event)
            self._queue.task_done()
            if self._needs_snapshot and self._latest_state is not None:
                state = self._latest_state
                self._latest_state = None
                self._needs_snapshot = False
                self._seq += 1
                await send(DispatchEvent(self.run_id, self._seq, "state_sync", {"agent_event": state}))

    def close(self) -> None:
        self._closed = True
