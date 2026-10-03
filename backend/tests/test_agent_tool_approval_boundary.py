from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.approval_context import approval_enabled
from app.agent.nodes import tools as tools_node
from app.tools.card import make_card
from app.tools import tool_search


def _approval_card(handler=lambda **_: "ran"):
    return make_card(
        package="demo",
        name="protected_action",
        handler=handler,
        summary="protected action",
        description="protected action",
        display_name="protected action",
        display_icon="x",
        concurrency="barrier",
        source="plugin",
    )


def _state(*ids: str):
    return {
        "messages": [AIMessage(content="", tool_calls=[
            {"id": call_id, "name": "protected_action", "args": {}}
            for call_id in ids
        ])],
        "scope": {"run_id": "run-1"},
    }


def _decision(outcome: str):
    return SimpleNamespace(
        outcome=outcome,
        reason="approval required",
        reason_code="approval_required",
        preview=None,
        to_tool_error_message=lambda: "approval required",
    )


@pytest.mark.asyncio
async def test_no_approval_channel_returns_failure_without_running_handler(monkeypatch, tmp_path):
    async def bind(_session_id):
        return tmp_path

    card = _approval_card()
    monkeypatch.setattr(tools_node, "abind_session_runtime", bind)
    monkeypatch.setattr(tools_node, "dispatch_pre_tool_call", lambda **_: None)
    monkeypatch.setattr(tools_node, "_resolve_effective_card_and_args", lambda _name, args: (card, args, None))
    monkeypatch.setattr(tools_node, "build_execution_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(tools_node, "evaluate_policy", lambda _ctx: _decision("needs_approval"))
    monkeypatch.setattr(tools_node, "run_tool_calls", lambda *_args, **_kwargs: pytest.fail("handler ran"))
    token = approval_enabled.set(False)
    try:
        result = await tools_node.call_tools(_state("call-1"), {"configurable": {"thread_id": "session-1"}})
    finally:
        approval_enabled.reset(token)

    assert len(result["messages"]) == 1
    assert result["messages"][0].tool_call_id == "call-1"
    assert result["messages"][0].additional_kwargs["reason_code"] == "approval_required"


@pytest.mark.asyncio
async def test_no_approval_channel_preserves_other_calls_in_mixed_batch(monkeypatch, tmp_path):
    async def bind(_session_id):
        return tmp_path

    card = _approval_card()
    decisions = iter([_decision("allow"), _decision("needs_approval")])
    executed = []

    def run_calls(calls, _session_id, **_kwargs):
        executed.extend(call["id"] for call in calls)
        return [ToolMessage(content="ran", tool_call_id=call["id"], name=call["name"]) for call in calls]

    monkeypatch.setattr(tools_node, "abind_session_runtime", bind)
    monkeypatch.setattr(tools_node, "bind_session_runtime", lambda _session_id: None)
    monkeypatch.setattr(tools_node, "dispatch_pre_tool_call", lambda **_: None)
    monkeypatch.setattr(tools_node, "_resolve_effective_card_and_args", lambda _name, args: (card, args, None))
    monkeypatch.setattr(tools_node, "build_execution_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(tools_node, "evaluate_policy", lambda _ctx: next(decisions))
    monkeypatch.setattr(tools_node, "run_tool_calls", run_calls)
    monkeypatch.setattr(
        tools_node,
        "execute_sync",
        lambda _dependency, function, **_kwargs: SimpleNamespace(ok=True, value=function(), error=None),
    )
    token = approval_enabled.set(False)
    try:
        result = await tools_node.call_tools(_state("ordinary", "protected"), {"configurable": {"thread_id": "session-1"}})
    finally:
        approval_enabled.reset(token)

    assert executed == ["ordinary"]
    assert {message.tool_call_id for message in result["messages"]} == {"ordinary", "protected"}
    protected = next(message for message in result["messages"] if message.tool_call_id == "protected")
    assert protected.additional_kwargs["reason_code"] == "approval_required"


@pytest.mark.asyncio
async def test_interactive_allow_passes_only_matching_call_grant(monkeypatch, tmp_path):
    async def bind(_session_id):
        return tmp_path

    card = _approval_card()
    decisions = iter([_decision("allow"), _decision("needs_approval"), _decision("allow")])
    captured = {}

    def run_calls(calls, _session_id, **kwargs):
        captured.update(kwargs["execution_grants"])
        return [ToolMessage(content="ran", tool_call_id=call["id"], name=call["name"]) for call in calls]

    monkeypatch.setattr(tools_node, "abind_session_runtime", bind)
    monkeypatch.setattr(tools_node, "bind_session_runtime", lambda _session_id: None)
    monkeypatch.setattr(tools_node, "dispatch_pre_tool_call", lambda **_: None)
    monkeypatch.setattr(tools_node, "_resolve_effective_card_and_args", lambda _name, args: (card, args, None))
    monkeypatch.setattr(tools_node, "build_execution_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(tools_node, "evaluate_policy", lambda _ctx: next(decisions))
    monkeypatch.setattr(tools_node, "interrupt", lambda _payload: "allow")
    monkeypatch.setattr(tools_node, "run_tool_calls", run_calls)
    monkeypatch.setattr(
        tools_node,
        "execute_sync",
        lambda _dependency, function, **_kwargs: SimpleNamespace(ok=True, value=function(), error=None),
    )
    token = approval_enabled.set(True)
    try:
        result = await tools_node.call_tools(_state("ordinary", "approved"), {"configurable": {"thread_id": "session-1"}})
    finally:
        approval_enabled.reset(token)

    assert len(result["messages"]) == 2
    assert captured["ordinary"]["approval_granted"] is False
    assert captured["approved"]["approval_granted"] is True


@pytest.mark.asyncio
async def test_interactive_denial_does_not_run_call(monkeypatch, tmp_path):
    async def bind(_session_id):
        return tmp_path

    card = _approval_card()
    monkeypatch.setattr(tools_node, "abind_session_runtime", bind)
    monkeypatch.setattr(tools_node, "dispatch_pre_tool_call", lambda **_: None)
    monkeypatch.setattr(tools_node, "_resolve_effective_card_and_args", lambda _name, args: (card, args, None))
    monkeypatch.setattr(tools_node, "build_execution_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(tools_node, "evaluate_policy", lambda _ctx: _decision("needs_approval"))
    monkeypatch.setattr(tools_node, "interrupt", lambda _payload: "deny")
    monkeypatch.setattr(tools_node, "run_tool_calls", lambda *_args, **_kwargs: pytest.fail("handler ran"))
    token = approval_enabled.set(True)
    try:
        result = await tools_node.call_tools(_state("call-1"), {"configurable": {"thread_id": "session-1"}})
    finally:
        approval_enabled.reset(token)

    assert result["messages"][0].additional_kwargs["reason_code"] == "user_denied"


def test_batch_executor_defaults_to_unapproved_and_scopes_grant_by_call_id(monkeypatch):
    calls: list[str] = []
    card = _approval_card(lambda **_: calls.append("ran") or "ran")
    monkeypatch.setattr(tool_search, "_cards_for_dispatch", lambda: ([card], tool_search.ToolSearchConfig(enabled=False)))
    monkeypatch.setattr("app.storage.project.bind_session_runtime", lambda _session_id: None)
    monkeypatch.setattr(
        "app.tools.policy.gate_before_handler",
        lambda _card, _args, **kwargs: _decision("allow" if kwargs["approval_granted"] else "needs_approval"),
    )
    requested = [
        {"id": "allowed", "name": card.name, "args": {}},
        {"id": "unapproved", "name": card.name, "args": {}},
    ]
    results = tool_search.run_tool_calls(
        requested,
        "session-1",
        process_for_cache=lambda message, _session_id: message,
        execution_grants={"allowed": {"approval_granted": True}},
    )

    assert calls == ["ran"]
    assert [message.tool_call_id for message in results] == ["allowed", "unapproved"]
    assert results[0].additional_kwargs["tool_status"] == "completed"
    assert results[1].additional_kwargs["tool_status"] == "failed"
