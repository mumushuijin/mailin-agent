from __future__ import annotations

from app.tools.card import make_card, resolve_concurrency


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
