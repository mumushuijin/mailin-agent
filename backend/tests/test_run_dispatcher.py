import asyncio

import pytest

from app.agent.streaming.dispatcher import RunEventDispatcher
from app.agent.hooks import AGENT_NODE_START, dispatch_observe_nonblocking, register_hook, get_hook_manager
from app.agent.graph import _node_lifecycle
from app.agent.hooks import AGENT_NODE_END
from langgraph.errors import GraphInterrupt


@pytest.mark.asyncio
async def test_dispatcher_preserves_order_and_does_not_wait_for_slow_consumer():
    dispatcher = RunEventDispatcher("run-1", maxsize=2)
    assert dispatcher.publish_nowait("node_start", {"step_id": "s1"})
    assert dispatcher.publish_nowait("node_end", {"step_id": "s1"})
    assert not dispatcher.publish_nowait("heartbeat", {"stale": True})
    received = []

    async def send(event):
        received.append(event)

    task = asyncio.create_task(dispatcher.run(send))
    await asyncio.sleep(0)
    dispatcher.close()
    await task
    assert [item.seq for item in received] == [1, 2]
    assert [item.kind for item in received] == ["node_start", "node_end"]


@pytest.mark.asyncio
async def test_dispatcher_coalesces_overflow_to_latest_snapshot():
    dispatcher = RunEventDispatcher("run-2", maxsize=1)
    assert dispatcher.publish_nowait("stage", {"status": "started"})
    assert not dispatcher.publish_nowait("step_finish", {}, state={"revision": 2})
    received = []

    async def send(event):
        received.append(event)

    consumer = asyncio.create_task(dispatcher.run(send))
    await asyncio.sleep(0.01)
    dispatcher.close()
    await asyncio.wait_for(consumer, 1)
    sync = [item for item in received if item.kind == "state_sync"]
    assert sync and sync[0].payload["agent_event"] == {"revision": 2}


@pytest.mark.asyncio
async def test_dispatcher_waits_for_capacity_to_preserve_text_chunks():
    dispatcher = RunEventDispatcher("run-3", maxsize=1)
    received = []
    gate = asyncio.Event()
    started = asyncio.Event()

    async def send(event):
        if not started.is_set():
            started.set()
            await gate.wait()
        received.append(event)

    await dispatcher.publish("chunk", {"text": "first"})
    consumer = asyncio.create_task(dispatcher.run(send))
    await asyncio.wait_for(started.wait(), 1)
    await dispatcher.publish("chunk", {"text": "second"})
    gate.set()
    await asyncio.sleep(0.01)
    dispatcher.close()
    await asyncio.wait_for(consumer, 1)
    assert [item.payload["text"] for item in received] == ["first", "second"]


def test_lifecycle_hook_callback_runs_off_caller_thread():
    import threading
    from threading import Event

    manager = get_hook_manager()
    manager.clear()
    done = Event()
    callback_threads = []

    def hook(**_kwargs):
        callback_threads.append(threading.current_thread().name)
        done.set()

    register_hook(AGENT_NODE_START, hook)
    caller_thread = threading.current_thread().name
    dispatch_observe_nonblocking(AGENT_NODE_START, status="running")
    assert done.wait(1)
    assert callback_threads[0] != caller_thread
    manager.clear()


def test_node_lifecycle_pairs_interruption_and_preserves_status_and_duration():
    manager = get_hook_manager()
    manager.clear()
    start = []
    end = []
    from threading import Event
    started_event = Event()
    ended_event = Event()

    def on_start(**kwargs):
        start.append(kwargs)
        started_event.set()

    def on_end(**kwargs):
        end.append(kwargs)
        ended_event.set()

    register_hook(AGENT_NODE_START, on_start)
    register_hook(AGENT_NODE_END, on_end)
    try:
        with pytest.raises(GraphInterrupt):
            with _node_lifecycle(AGENT_NODE_START, AGENT_NODE_END, scope={"run_id": "r"}, node="agent", input_summary="safe input"):
                raise GraphInterrupt([])
        assert started_event.wait(1)
        assert ended_event.wait(1)
        assert start[0]["status"] == "running"
        assert end[0]["status"] == "interrupted"
        assert end[0]["duration_ms"] >= 0
    finally:
        manager.clear()
