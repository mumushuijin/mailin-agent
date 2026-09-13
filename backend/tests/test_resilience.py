from __future__ import annotations

import time

import pytest

from app.resilience import (
    CallContext,
    CircuitState,
    ResiliencePolicy,
    execute,
    execute_sync,
    get_registry,
)
from app.resilience.circuit import CircuitBreaker


@pytest.fixture(autouse=True)
def _reset_registry():
    get_registry().reset()
    yield
    get_registry().reset()


def test_execute_sync_success():
    outcome = execute_sync("test.ok", lambda: "hello")
    assert outcome.ok is True
    assert outcome.value == "hello"
    assert outcome.attempts == 1
    assert outcome.circuit_state == CircuitState.CLOSED


def test_execute_sync_retries_transient_error():
    calls = {"n": 0}

    def _flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("peer closed connection")
        return "recovered"

    policy = ResiliencePolicy(
        dependency_id="test.flaky",
        max_retries=1,
        retry_backoff_seconds=0.0,
        transient_markers=("peer closed connection",),
    )
    outcome = execute_sync("test.flaky", _flaky, policy=policy)
    assert outcome.ok is True
    assert outcome.value == "recovered"
    assert outcome.attempts == 2
    assert calls["n"] == 2


def test_execute_sync_no_retry_on_non_transient():
    calls = {"n": 0}

    def _bad() -> str:
        calls["n"] += 1
        raise ValueError("invalid args")

    policy = ResiliencePolicy(
        dependency_id="test.bad",
        max_retries=2,
        transient_markers=("timeout",),
    )
    outcome = execute_sync("test.bad", _bad, policy=policy)
    assert outcome.ok is False
    assert "invalid args" in (outcome.error or "")
    assert calls["n"] == 1


def test_circuit_opens_after_threshold():
    breaker = CircuitBreaker("dep", failure_threshold=2, open_seconds=60.0)
    breaker.record_failure()
    assert breaker.allow_request() is True
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False


def test_circuit_half_open_after_cooldown():
    breaker = CircuitBreaker("dep", failure_threshold=1, open_seconds=0.05)
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    time.sleep(0.06)
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.allow_request() is True
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


def test_execute_sync_respects_circuit_open():
    policy = ResiliencePolicy(
        dependency_id="test.circuit",
        circuit_failure_threshold=1,
        circuit_open_seconds=60.0,
        transient_markers=(),
        max_retries=0,
    )

    outcome1 = execute_sync("test.circuit", lambda: (_ for _ in ()).throw(RuntimeError("boom")), policy=policy)
    assert outcome1.ok is False

    outcome2 = execute_sync("test.circuit", lambda: "should not run", policy=policy)
    assert outcome2.ok is False
    assert "熔断" in (outcome2.error or "")
    assert outcome2.circuit_state == CircuitState.OPEN


def test_fallback_on_failure():
    policy = ResiliencePolicy(
        dependency_id="test.fallback",
        max_retries=0,
        fallback=lambda: "fallback-value",
    )
    outcome = execute_sync(
        "test.fallback",
        lambda: (_ for _ in ()).throw(RuntimeError("fail")),
        policy=policy,
    )
    assert outcome.ok is True
    assert outcome.degraded is True
    assert outcome.value == "fallback-value"


async def test_execute_async_success():
    async def _ok() -> str:
        return "async-hello"

    outcome = await execute("test.async.ok", _ok)
    assert outcome.ok is True
    assert outcome.value == "async-hello"
    assert outcome.attempts == 1


async def test_execute_async_retries_transient_error():
    calls = {"n": 0}

    async def _flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("timed out")
        return "async-recovered"

    policy = ResiliencePolicy(
        dependency_id="test.async.flaky",
        max_retries=1,
        retry_backoff_seconds=0.0,
        transient_markers=("timed out",),
    )
    outcome = await execute("test.async.flaky", _flaky, policy=policy)
    assert outcome.ok is True
    assert outcome.value == "async-recovered"
    assert outcome.attempts == 2


async def test_execute_async_circuit_open_fast_fail():
    policy = ResiliencePolicy(
        dependency_id="test.async.circuit",
        circuit_failure_threshold=1,
        circuit_open_seconds=60.0,
        max_retries=0,
    )

    async def _boom() -> str:
        raise RuntimeError("boom")

    out1 = await execute("test.async.circuit", _boom, policy=policy)
    assert out1.ok is False

    async def _never() -> str:
        return "should not run"

    out2 = await execute("test.async.circuit", _never, policy=policy)
    assert out2.ok is False
    assert "熔断" in (out2.error or "")
    assert out2.circuit_state == CircuitState.OPEN


def test_execute_sync_timeout_thread_keeps_contextvars():
    from contextvars import ContextVar

    flag: ContextVar[str] = ContextVar("resilience_ctx_flag", default="missing")
    flag.set("bound")

    def _read() -> str:
        return flag.get()

    policy = ResiliencePolicy(
        dependency_id="test.ctx",
        timeout_seconds=2.0,
        max_retries=0,
    )
    outcome = execute_sync("test.ctx", _read, policy=policy)
    assert outcome.ok is True
    assert outcome.value == "bound"
