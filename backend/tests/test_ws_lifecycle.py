from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.streaming.events import AgentEvent
from app.maintenance.runner import MaintenanceRunner
from app.maintenance.types import MaintenanceResult
from app.services.ws_chat_service import WsChatService


@pytest.mark.asyncio
async def test_sediment_skipped_does_not_notify():
    runner = MaintenanceRunner()
    pushes: list[MaintenanceResult] = []

    async def capture(result: MaintenanceResult, **_kwargs) -> None:
        pushes.append(result)

    runner.notify = capture  # type: ignore[method-assign]

    with patch("app.maintenance.runner.sediment_would_run", return_value=False):
        result = await runner.run_sediment(notify=True)

    assert result.status == "skipped"
    assert pushes == []


@pytest.mark.asyncio
async def test_schedule_sediment_debounced():
    runner = MaintenanceRunner()
    calls = 0

    async def fake_run(*, notify: bool = True):
        nonlocal calls
        calls += 1
        return MaintenanceResult(kind="sediment", message="skip", status="skipped")

    with patch.object(runner, "run_sediment", side_effect=fake_run):
        runner.schedule_sediment()
        runner.schedule_sediment()
        await asyncio.sleep(0.05)

    assert calls == 1


@pytest.mark.asyncio
async def test_sediment_started_always_paired_with_done():
    runner = MaintenanceRunner()
    pushes: list[MaintenanceResult] = []

    async def capture(result: MaintenanceResult, **_kwargs) -> None:
        pushes.append(result)

    runner.notify = capture  # type: ignore[method-assign]

    with patch("app.maintenance.runner.sediment_would_run", return_value=True), patch(
        "app.maintenance.runner.run_memory_sediment",
        return_value=MaintenanceResult(
            kind="sediment",
            message="已沉淀 2 条",
            status="done",
            success=True,
        ),
    ):
        result = await runner.run_sediment(notify=True)

    assert result.status == "done"
    assert len(pushes) == 2
    assert pushes[0].status == "started"
    assert pushes[1].status == "done"
    assert pushes[0].task_id == pushes[1].task_id


@pytest.mark.asyncio
async def test_sediment_timeout_emits_failed():
    runner = MaintenanceRunner()
    pushes: list[MaintenanceResult] = []

    async def capture(result: MaintenanceResult, **_kwargs) -> None:
        pushes.append(result)

    runner.notify = capture  # type: ignore[method-assign]

    def slow_sediment() -> MaintenanceResult:
        time.sleep(0.3)
        return MaintenanceResult(kind="sediment", message="不应到达", status="done")

    with patch("app.maintenance.runner.sediment_would_run", return_value=True), patch(
        "app.maintenance.runner.SEDIMENT_TIMEOUT_SECONDS", 0.05
    ), patch(
        "app.maintenance.runner.run_memory_sediment",
        side_effect=slow_sediment,
    ):
        result = await runner.run_sediment(notify=True)

    assert result.status == "failed"
    assert len(pushes) == 2
    assert pushes[0].status == "started"
    assert pushes[1].status == "failed"
    assert pushes[0].task_id == pushes[1].task_id


@pytest.mark.asyncio
async def test_schedule_sediment_coalesced():
    runner = MaintenanceRunner()
    gate = asyncio.Event()
    started = asyncio.Event()
    call_count = 0

    def blocking_sediment() -> MaintenanceResult:
        nonlocal call_count
        call_count += 1
        started.set()
        while not gate.is_set():
            time.sleep(0.01)
        return MaintenanceResult(kind="sediment", message="ok", status="done")

    with patch("app.maintenance.runner.sediment_would_run", return_value=True), patch(
        "app.maintenance.runner.run_memory_sediment",
        side_effect=blocking_sediment,
    ):
        runner.schedule_sediment()
        runner.schedule_sediment()
        await asyncio.wait_for(started.wait(), timeout=1)
        assert runner._sediment_task is not None
        gate.set()
        await asyncio.wait_for(runner._sediment_task, timeout=2)
        assert call_count == 1


@pytest.mark.asyncio
async def test_memory_nudge_failure_still_notifies_terminal():
    runner = MaintenanceRunner()
    pushes: list[MaintenanceResult] = []

    async def capture(result: MaintenanceResult, **_kwargs) -> None:
        pushes.append(result)

    runner.notify = capture  # type: ignore[method-assign]

    with patch(
        "app.maintenance.runner.run_memory_nudge",
        side_effect=RuntimeError("boom"),
    ):
        result = await runner.run_memory_nudge_for_session("sess-1")

    assert result.status == "failed"
    assert len(pushes) == 2
    assert pushes[0].status == "started"
    assert pushes[1].status == "failed"


@pytest.mark.asyncio
async def test_ws_cancel_emits_terminal_events_when_stream_silent():
    manager = MagicMock()
    manager.send_event = AsyncMock()
    manager.subscribe_session = MagicMock()

    chat_service = MagicMock()

    async def failing_iter(*_args, **_kwargs):
        raise asyncio.CancelledError()
        yield AgentEvent("session", {"session_id": "s1"})  # pragma: no cover

    chat_service.iter_chat_events = failing_iter
    chat_service.repair_cancelled_checkpoint = AsyncMock()

    svc = WsChatService(manager, chat_service)
    await svc._execute_run(1, "run-1", "hi", "sess-1")

    sent_types = [call.args[1].type for call in manager.send_event.await_args_list]
    assert "error" in sent_types
    assert "done" in sent_types
    cancelled_errors = [
        call.args[1]
        for call in manager.send_event.await_args_list
        if call.args[1].type == "error"
    ]
    assert cancelled_errors[0].data.get("cancelled") is True


@pytest.mark.asyncio
async def test_cancel_run_cancels_approval_future_instead_of_deny():
    manager = MagicMock()
    chat_service = MagicMock()
    chat_service.repair_cancelled_checkpoint = AsyncMock()
    svc = WsChatService(manager, chat_service)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    svc._approval_futures["run-1"] = future

    done_task = asyncio.create_task(asyncio.sleep(0))
    svc._active_runs["run-1"] = done_task
    svc._run_sessions["run-1"] = "sess-1"

    await svc.cancel_run("run-1")

    assert future.cancelled()
    chat_service.repair_cancelled_checkpoint.assert_awaited_once_with("sess-1")


def test_push_maintenance_background_payload():
    from app.services.push_service import push_maintenance

    async def _run() -> None:
        mgr = MagicMock()
        mgr.broadcast_all = AsyncMock()
        with patch("app.services.push_service.get_ws_manager", return_value=mgr):
            await push_maintenance(
                "sediment",
                "正在整理记忆沉淀…",
                status="started",
                task_id="tid-1",
            )
        event = mgr.broadcast_all.await_args.args[0]
        assert event.type == "background"
        assert event.data["kind"] == "sediment"
        assert event.data["status"] == "started"
        assert event.data["task_id"] == "tid-1"

    asyncio.run(_run())


def test_ws_background_event_has_no_run_id():
    from app.agent.streaming.events import to_ws

    payload = to_ws(
        AgentEvent(
            "background",
            {
                "kind": "sediment",
                "status": "started",
                "message": "正在整理记忆沉淀…",
                "success": True,
                "task_id": "abc",
            },
        )
    )
    assert payload["type"] == "background"
    assert payload["data"]["task_id"] == "abc"
    assert "run_id" not in payload
