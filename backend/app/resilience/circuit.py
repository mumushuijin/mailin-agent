from __future__ import annotations

from datetime import datetime, timezone

import pybreaker

from app.resilience.types import CircuitState

CircuitOpenError = pybreaker.CircuitBreakerError

_STATE_MAP = {
    "closed": CircuitState.CLOSED,
    "open": CircuitState.OPEN,
    "half-open": CircuitState.HALF_OPEN,
}


class CircuitBreaker:
    """pybreaker 适配器。

    对外提供 record_success / record_failure / allow_request / state 接口，
    内部由 pybreaker 维护状态机（连续失败计数、开路、冷却、半开探测）。
    executor 通过 allow_request 判定是否放行、执行后 record_* 上报结果，
    从而同时兼容同步与原生 asyncio（不依赖 pybreaker 基于 tornado 的 call_async）。

    throw_new_error_on_trip=False：record_failure 内部触发熔断时不额外抛
    CircuitBreakerError，避免污染上报逻辑。
    """

    def __init__(
        self,
        dependency_id: str,
        *,
        failure_threshold: int = 5,
        open_seconds: float = 30.0,
    ) -> None:
        self.dependency_id = dependency_id
        self.open_seconds = max(0.0, open_seconds)
        self._breaker = pybreaker.CircuitBreaker(
            fail_max=max(1, failure_threshold),
            reset_timeout=self.open_seconds,
            name=dependency_id,
            throw_new_error_on_trip=False,
        )

    def _cooldown_elapsed(self) -> bool:
        opened_at = getattr(self._breaker._state_storage, "opened_at", None)
        if opened_at is None:
            return False
        return (datetime.now(timezone.utc) - opened_at).total_seconds() >= self.open_seconds

    def _effective_state_name(self) -> str:
        name = self._breaker.current_state
        # pybreaker 仅在发生调用时才由 open 迁移到 half-open；
        # 为遥测/allow_request 提供无副作用的时间判断。
        if name == "open" and self._cooldown_elapsed():
            return "half-open"
        return name

    @property
    def state(self) -> CircuitState:
        return _STATE_MAP.get(self._effective_state_name(), CircuitState.CLOSED)

    def allow_request(self) -> bool:
        return self._effective_state_name() != "open"

    def record_success(self) -> None:
        try:
            self._breaker.call(lambda: None)
        except pybreaker.CircuitBreakerError:
            pass

    def record_failure(self) -> CircuitState:
        def _raise() -> None:
            raise RuntimeError("recorded failure")

        try:
            self._breaker.call(_raise)
        except Exception:
            pass
        return self.state
