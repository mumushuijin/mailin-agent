from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
    wait_none,
)

from app.resilience.circuit import CircuitBreaker
from app.resilience.policy import ResiliencePolicy
from app.resilience.registry import get_registry
from app.resilience.types import CallContext, CircuitState, Outcome

logger = logging.getLogger(__name__)

T = TypeVar("T")

OnRetry = Callable[[int, BaseException], None]
PreAttempt = Callable[[], None]


def is_transient_error(exc: BaseException, markers: tuple[str, ...]) -> bool:
    text = str(exc).lower()
    return any(marker.lower() in text for marker in markers)


def _resolve_policy(dependency_id: str, policy: ResiliencePolicy | None) -> ResiliencePolicy:
    resolved = policy or get_registry().get_policy(dependency_id)
    if resolved.dependency_id != dependency_id:
        return ResiliencePolicy(
            dependency_id=dependency_id,
            timeout_seconds=resolved.timeout_seconds,
            max_retries=resolved.max_retries,
            retry_backoff_seconds=resolved.retry_backoff_seconds,
            retry_jitter_seconds=resolved.retry_jitter_seconds,
            transient_markers=resolved.transient_markers,
            retry_on=resolved.retry_on,
            circuit_failure_threshold=resolved.circuit_failure_threshold,
            circuit_open_seconds=resolved.circuit_open_seconds,
            fallback=resolved.fallback,
        )
    return resolved


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _run_with_timeout(fn: Callable[[], T], timeout: float | None) -> T:
    if timeout is None or timeout <= 0:
        return fn()
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="resilience") as pool:
        future = pool.submit(ctx.run, fn)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            raise TimeoutError(f"依赖调用超时（{timeout}s）") from exc


def _retry_wait(policy: ResiliencePolicy):
    if policy.retry_backoff_seconds > 0:
        return wait_exponential_jitter(
            initial=policy.retry_backoff_seconds,
            jitter=max(0.0, policy.retry_jitter_seconds),
        )
    return wait_none()


def _retry_common_kwargs(policy: ResiliencePolicy, on_retry: OnRetry | None) -> dict:
    kwargs: dict = {
        "stop": stop_after_attempt(policy.total_attempts()),
        "wait": _retry_wait(policy),
        "retry": retry_if_exception(policy.is_retryable),
        "reraise": True,
    }
    if on_retry is not None:

        def _before_sleep(retry_state) -> None:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            if exc is not None:
                on_retry(retry_state.attempt_number, exc)

        kwargs["before_sleep"] = _before_sleep
    return kwargs


def _failure_outcome(
    policy: ResiliencePolicy,
    *,
    error: str,
    dependency_id: str,
    attempts: int,
    latency_ms: float,
    circuit_state: CircuitState,
) -> Outcome[T]:
    if policy.fallback is not None:
        try:
            return Outcome.failure(
                error,
                dependency_id=dependency_id,
                attempts=attempts,
                latency_ms=latency_ms,
                circuit_state=circuit_state,
                fallback_value=policy.fallback(),
            )
        except Exception as fallback_exc:
            logger.warning("resilience fallback 失败 [%s]: %s", dependency_id, fallback_exc)
    return Outcome.failure(
        error,
        dependency_id=dependency_id,
        attempts=attempts,
        latency_ms=latency_ms,
        circuit_state=circuit_state,
    )


def _circuit_open_outcome(
    resolved: ResiliencePolicy,
    breaker: CircuitBreaker,
    dependency_id: str,
    attempts: int,
    started: float,
    context: CallContext | None,
) -> Outcome[T]:
    logger.warning("resilience circuit open: %s ctx=%s", dependency_id, context)
    return _failure_outcome(
        resolved,
        error=f"依赖 '{dependency_id}' 熔断中，请稍后重试",
        dependency_id=dependency_id,
        attempts=attempts,
        latency_ms=_elapsed_ms(started),
        circuit_state=CircuitState.OPEN,
    )


def execute_sync(
    dependency_id: str,
    fn: Callable[[], T],
    *,
    policy: ResiliencePolicy | None = None,
    context: CallContext | None = None,
    on_retry: OnRetry | None = None,
    pre_attempt: PreAttempt | None = None,
) -> Outcome[T]:
    """同步执行依赖调用：熔断（pybreaker）→ 重试（tenacity）→ 超时 → 统一 Outcome。"""
    resolved = _resolve_policy(dependency_id, policy)
    breaker = get_registry().get_breaker(dependency_id, resolved)
    started = time.monotonic()
    counter = {"n": 0}

    if not breaker.allow_request():
        return _circuit_open_outcome(resolved, breaker, dependency_id, 0, started, context)

    def _one_attempt() -> T:
        counter["n"] += 1
        if pre_attempt is not None:
            pre_attempt()
        return _run_with_timeout(fn, resolved.timeout_seconds)

    retrying = Retrying(**_retry_common_kwargs(resolved, on_retry))

    try:
        value = retrying(_one_attempt)
    except Exception as exc:
        last_error = str(exc)
        circuit_state = breaker.record_failure()
        logger.warning(
            "resilience call failed [%s] attempts=%d: %s",
            dependency_id,
            counter["n"],
            last_error,
            extra={"session_id": getattr(context, "session_id", None)},
        )
        return _failure_outcome(
            resolved,
            error=last_error,
            dependency_id=dependency_id,
            attempts=counter["n"],
            latency_ms=_elapsed_ms(started),
            circuit_state=circuit_state,
        )

    breaker.record_success()
    return Outcome.success(
        value,
        dependency_id=dependency_id,
        attempts=counter["n"],
        latency_ms=_elapsed_ms(started),
        circuit_state=breaker.state,
    )


async def execute(
    dependency_id: str,
    fn: Callable[[], Awaitable[T]],
    *,
    policy: ResiliencePolicy | None = None,
    context: CallContext | None = None,
    on_retry: OnRetry | None = None,
    pre_attempt: PreAttempt | None = None,
) -> Outcome[T]:
    """异步执行依赖调用（逻辑与 execute_sync 对齐）。"""
    resolved = _resolve_policy(dependency_id, policy)
    breaker = get_registry().get_breaker(dependency_id, resolved)
    started = time.monotonic()
    counter = {"n": 0}

    if not breaker.allow_request():
        return _circuit_open_outcome(resolved, breaker, dependency_id, 0, started, context)

    async def _one_attempt() -> T:
        counter["n"] += 1
        if pre_attempt is not None:
            pre_attempt()
        coro = fn()
        if resolved.timeout_seconds and resolved.timeout_seconds > 0:
            return await asyncio.wait_for(coro, timeout=resolved.timeout_seconds)
        return await coro

    async_retrying = AsyncRetrying(**_retry_common_kwargs(resolved, on_retry))

    try:
        value = await async_retrying(_one_attempt)
    except Exception as exc:
        last_error = str(exc)
        circuit_state = breaker.record_failure()
        logger.warning(
            "resilience async call failed [%s] attempts=%d: %s",
            dependency_id,
            counter["n"],
            last_error,
        )
        return _failure_outcome(
            resolved,
            error=last_error,
            dependency_id=dependency_id,
            attempts=counter["n"],
            latency_ms=_elapsed_ms(started),
            circuit_state=circuit_state,
        )

    breaker.record_success()
    return Outcome.success(
        value,
        dependency_id=dependency_id,
        attempts=counter["n"],
        latency_ms=_elapsed_ms(started),
        circuit_state=breaker.state,
    )
