"""ask_user interrupt 协议：mode / interrupt_id / handoff。"""

from __future__ import annotations

from app.agent.nodes.tools import ASK_USER_HANDOFF, _format_ask_user_resume


def test_answer_and_continue_compatible():
    text, handoff = _format_ask_user_resume(
        {"kind": "ask_user", "answer": "选 A", "interrupt_id": "iid-1"},
        options=["选 A", "选 B"],
        allow_multiple=False,
        mode="answer_and_continue",
        interrupt_id="iid-1",
    )
    assert text == "用户回答：选 A"
    assert handoff is False


def test_stale_interrupt_id_rejected():
    text, handoff = _format_ask_user_resume(
        {"kind": "ask_user", "answer": "选 A", "interrupt_id": "old"},
        options=[],
        allow_multiple=False,
        mode="answer_and_continue",
        interrupt_id="new",
    )
    assert text is None
    assert handoff is False


def test_handoff_and_stop_ack():
    text, handoff = _format_ask_user_resume(
        {"kind": "ask_user", "mode": "handoff_and_stop", "ack": True, "interrupt_id": "h1"},
        options=[],
        allow_multiple=False,
        mode="handoff_and_stop",
        interrupt_id="h1",
    )
    assert text == ASK_USER_HANDOFF
    assert handoff is True


def test_cancel_or_empty_fail_closed():
    text, handoff = _format_ask_user_resume(
        {"kind": "ask_user", "answer": None, "interrupt_id": "x"},
        options=[],
        allow_multiple=False,
        mode="answer_and_continue",
        interrupt_id="x",
    )
    assert text is None
