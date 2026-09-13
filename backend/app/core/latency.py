from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


LATENCY_STAGE_ACCEPTED = "accepted"
LATENCY_STAGE_CONTEXT_PREPARE = "context_prepare"
LATENCY_STAGE_MODEL_STREAM = "model_stream"
LATENCY_STAGE_TOOL_BATCH = "tool_batch"
LATENCY_STAGE_USAGE_SNAPSHOT = "usage_snapshot"
LATENCY_STAGE_POSTPROCESS = "postprocess"
LATENCY_STAGE_HISTORY_LOAD = "history_load"


@dataclass
class RunLatency:
    """Small per-run timing helper for stream events, logs, and tests."""

    run_id: str | None
    session_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    _stage_starts: dict[str, float] = field(default_factory=dict)
    stage_durations_ms: dict[str, float] = field(default_factory=dict)

    def bind_session(self, session_id: str | None) -> None:
        if session_id:
            self.session_id = session_id

    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self.started_at) * 1000, 1)

    def start(self, stage: str, **extra: Any) -> dict[str, Any]:
        self._stage_starts[stage] = time.monotonic()
        return self._payload(stage, "started", **extra)

    def finish(self, stage: str, status: str = "done", **extra: Any) -> dict[str, Any]:
        started = self._stage_starts.pop(stage, None)
        if started is not None:
            self.stage_durations_ms[stage] = round((time.monotonic() - started) * 1000, 1)
        return self._payload(stage, status, duration_ms=self.stage_durations_ms.get(stage), **extra)

    def instant(self, stage: str, status: str = "done", **extra: Any) -> dict[str, Any]:
        return self._payload(stage, status, **extra)

    def summary(self, status: str = "success", **extra: Any) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": status,
            "total_duration_ms": self.elapsed_ms(),
            "stage_durations_ms": dict(self.stage_durations_ms),
        }
        payload.update(extra)
        return payload

    def _payload(self, stage: str, status: str, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": stage,
            "status": status,
            "elapsed_ms": self.elapsed_ms(),
        }
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.session_id:
            payload["session_id"] = self.session_id
        payload.update({k: v for k, v in extra.items() if v is not None})
        return payload
