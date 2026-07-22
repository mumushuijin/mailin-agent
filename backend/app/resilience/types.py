from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CallContext:
    """单次依赖调用的追踪上下文（可选）。"""

    session_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome(Generic[T]):
    """统一结果信封：成功、降级或失败。"""

    ok: bool
    value: T | None = None
    error: str | None = None
    degraded: bool = False
    attempts: int = 1
    latency_ms: float = 0.0
    circuit_state: CircuitState = CircuitState.CLOSED
    dependency_id: str = ""

    @classmethod
    def success(
        cls,
        value: T,
        *,
        dependency_id: str = "",
        attempts: int = 1,
        latency_ms: float = 0.0,
        circuit_state: CircuitState = CircuitState.CLOSED,
    ) -> Outcome[T]:
        return cls(
            ok=True,
            value=value,
            dependency_id=dependency_id,
            attempts=attempts,
            latency_ms=latency_ms,
            circuit_state=circuit_state,
        )

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        dependency_id: str = "",
        attempts: int = 1,
        latency_ms: float = 0.0,
        degraded: bool = False,
        circuit_state: CircuitState = CircuitState.CLOSED,
        fallback_value: T | None = None,
    ) -> Outcome[T]:
        if fallback_value is not None:
            return cls(
                ok=True,
                value=fallback_value,
                error=error,
                degraded=True,
                dependency_id=dependency_id,
                attempts=attempts,
                latency_ms=latency_ms,
                circuit_state=circuit_state,
            )
        return cls(
            ok=False,
            error=error,
            dependency_id=dependency_id,
            attempts=attempts,
            latency_ms=latency_ms,
            circuit_state=circuit_state,
        )
