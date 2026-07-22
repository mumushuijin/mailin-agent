from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.resilience.types import T

RetryPredicate = Callable[[BaseException], bool]


@dataclass
class ResiliencePolicy:
    """依赖调用的防护策略。"""

    dependency_id: str = ""
    timeout_seconds: float | None = None
    max_retries: int = 0
    retry_backoff_seconds: float = 0.5
    retry_jitter_seconds: float = 0.1
    transient_markers: tuple[str, ...] = ()
    retry_on: RetryPredicate | None = None
    circuit_failure_threshold: int = 5
    circuit_open_seconds: float = 30.0
    fallback: Callable[[], T] | None = None

    def total_attempts(self) -> int:
        return self.max_retries + 1

    def is_retryable(self, exc: BaseException) -> bool:
        if self.retry_on is not None:
            return self.retry_on(exc)
        if not self.transient_markers:
            return False
        text = str(exc).lower()
        return any(marker.lower() in text for marker in self.transient_markers)
