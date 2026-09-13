from __future__ import annotations

import time

from app.tools.card import make_card, resolve_concurrency
from app.tools.tool_search import ToolSearchConfig, run_tool_calls


def _card(name: str, concurrency: str = "barrier"):
    return make_card(
        package="demo",
        name=name,
        handler=lambda **_: "ok",
        summary=name,
        description=name,
        display_name=name,
        display_icon="x",
        concurrency=concurrency,  # type: ignore[arg-type]
    )


def test_read_is_safe_write_is_barrier():
    assert resolve_concurrency(_card("read_file", "safe"), {}) == "safe"
    assert resolve_concurrency(_card("write_file", "barrier"), {}) == "barrier"


def test_process_list_safe_kill_barrier():
    card = _card("process", "barrier")
    assert resolve_concurrency(card, {"action": "list"}) == "safe"
    assert resolve_concurrency(card, {"action": "kill"}) == "barrier"
    assert resolve_concurrency(card, {"action": "wait"}) == "barrier"


def test_safe_batch_finishes_before_following_barrier(monkeypatch):
    events: list[str] = []

    def handler(name: str, delay: float = 0.0):
        def _run(**_kwargs):
            events.append(f"{name}:start")
            time.sleep(delay)
            events.append(f"{name}:end")
            return name

        return _run

    cards = [
        make_card(
            package="demo",
            name="safe_a",
            handler=handler("safe_a", 0.03),
            summary="safe_a",
            description="safe_a",
            display_name="safe_a",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
        make_card(
            package="demo",
            name="safe_b",
            handler=handler("safe_b", 0.03),
            summary="safe_b",
            description="safe_b",
            display_name="safe_b",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
        make_card(
            package="demo",
            name="barrier",
            handler=handler("barrier"),
            summary="barrier",
            description="barrier",
            display_name="barrier",
            display_icon="x",
            concurrency="barrier",
            source="plugin",
        ),
    ]
    monkeypatch.setattr(
        "app.tools.tool_search._cards_for_dispatch",
        lambda: (cards, ToolSearchConfig(enabled=False)),
    )

    results = run_tool_calls(
        [
            {"name": "safe_a", "id": "1", "args": {}},
            {"name": "safe_b", "id": "2", "args": {}},
            {"name": "barrier", "id": "3", "args": {}},
        ],
        "sess-1",
        process_for_cache=lambda msg, _session_id: msg,
    )

    assert [msg.tool_call_id for msg in results] == ["1", "2", "3"]
    assert events.index("barrier:start") > events.index("safe_a:end")
    assert events.index("barrier:start") > events.index("safe_b:end")
