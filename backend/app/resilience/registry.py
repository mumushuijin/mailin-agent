from __future__ import annotations

import threading

from app.resilience.circuit import CircuitBreaker
from app.resilience.policy import ResiliencePolicy

_DEFAULT_POLICY = ResiliencePolicy()


class Registry:
    """依赖策略与熔断器注册表（进程单例）。"""

    def __init__(self) -> None:
        self._policies: dict[str, ResiliencePolicy] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def register_policy(self, policy: ResiliencePolicy) -> None:
        if not policy.dependency_id:
            raise ValueError("policy.dependency_id 不能为空")
        with self._lock:
            self._policies[policy.dependency_id] = policy

    def get_policy(self, dependency_id: str) -> ResiliencePolicy:
        with self._lock:
            return self._policies.get(dependency_id, _DEFAULT_POLICY)

    def get_breaker(self, dependency_id: str, policy: ResiliencePolicy) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(dependency_id)
            if breaker is None:
                breaker = CircuitBreaker(
                    dependency_id,
                    failure_threshold=policy.circuit_failure_threshold,
                    open_seconds=policy.circuit_open_seconds,
                )
                self._breakers[dependency_id] = breaker
            return breaker

    def reset(self) -> None:
        with self._lock:
            self._policies.clear()
            self._breakers.clear()


_registry = Registry()


def get_registry() -> Registry:
    return _registry
