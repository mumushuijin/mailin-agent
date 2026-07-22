from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from app.resilience.executor import execute_sync
from app.resilience.policy import ResiliencePolicy
from app.resilience.types import CallContext, Outcome

T = TypeVar("T")


def guarded_step(
    dependency_id: str,
    fn: Callable[[], T],
    fallback: Callable[[], T],
    *,
    session_id: str | None = None,
    policy: ResiliencePolicy | None = None,
) -> T:
    """执行可降级步骤：失败时返回 fallback，不向上抛异常。"""
    resolved = policy or ResiliencePolicy(
        dependency_id=dependency_id,
        max_retries=0,
        fallback=fallback,
    )
    outcome = execute_sync(
        dependency_id,
        fn,
        policy=resolved,
        context=CallContext(session_id=session_id),
    )
    if outcome.ok and outcome.value is not None:
        return outcome.value
    return fallback()


def outcome_value_or_fallback(outcome: Outcome[T], fallback: Callable[[], T]) -> T:
    if outcome.ok and outcome.value is not None:
        return outcome.value
    return fallback()
